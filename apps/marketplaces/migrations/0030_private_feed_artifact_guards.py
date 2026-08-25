from django.db import migrations


FORWARD_SQL = r'''

CREATE OR REPLACE FUNCTION mkt_feed_artifact_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    endpoint_row marketplaces_marketplacefeedendpoint%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
    expected_object_key text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_update_forbidden';
    END IF;

    SELECT * INTO owner_row
      FROM marketplaces_marketplaceaccount
     WHERE id = NEW.account_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO endpoint_row
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = NEW.run_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO tenant_row
      FROM tenants_tenant
     WHERE id = owner_row.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    IF owner_row.deleted_at IS NOT NULL
       OR owner_row.is_active IS NOT TRUE
       OR tenant_row.is_active IS NOT TRUE
       OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
       OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
       OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
       OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.source_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
       OR run_row.predecessor_artifact_id IS DISTINCT FROM endpoint_row.current_artifact_id
       OR run_row.artifact_upload_attempt IS DISTINCT FROM NEW.upload_attempt
       OR run_row.payload_sha256 IS NULL
       OR run_row.payload_sha256 = ''
       OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    expected_object_key := format(
        'private-feeds/v1/%s/%s/%s/feed.xml',
        NEW.endpoint_id::text,
        NEW.run_id::text,
        lpad(NEW.upload_attempt::text, 5, '0')
    );
    IF NEW.object_key IS DISTINCT FROM expected_object_key THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_artifact_guard_trg
    ON marketplaces_marketplacefeedartifact;
CREATE TRIGGER mkt_feed_artifact_guard_trg
BEFORE INSERT OR UPDATE ON marketplaces_marketplacefeedartifact
FOR EACH ROW EXECUTE FUNCTION mkt_feed_artifact_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_endpoint_art_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
BEGIN
    IF NEW.account_id IS DISTINCT FROM OLD.account_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_endpoint_owner_mutation_forbidden';
    END IF;

    IF OLD.current_artifact_id IS DISTINCT FROM NEW.current_artifact_id THEN
        IF OLD.current_artifact_id IS NOT NULL
           AND NEW.current_artifact_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_artifact_clear_forbidden';
        END IF;

        IF NEW.current_artifact_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;

        IF NEW.owner_identity_digest IS DISTINCT FROM OLD.owner_identity_digest
           OR NEW.capability_revision IS DISTINCT FROM OLD.capability_revision
           OR NEW.token_key_id IS DISTINCT FROM OLD.token_key_id
           OR NEW.previous_token_key_id IS DISTINCT FROM OLD.previous_token_key_id
           OR (
               NEW.storage_mode IS DISTINCT FROM OLD.storage_mode
               AND NOT (
                   OLD.storage_mode = 'legacy_bridge'
                   AND NEW.storage_mode = 'private_generation'
                   AND OLD.serve_enabled IS TRUE
                   AND NEW.serve_enabled IS TRUE
               )
           )
           OR NEW.profile_state IS DISTINCT FROM OLD.profile_state
           OR NEW.profile_revision IS DISTINCT FROM OLD.profile_revision
           OR NEW.serve_enabled IS DISTINCT FROM OLD.serve_enabled
           OR NEW.legacy_object_key IS DISTINCT FROM OLD.legacy_object_key
           OR NEW.legacy_profile_url IS DISTINCT FROM OLD.legacy_profile_url
           OR NEW.profile_fingerprint IS DISTINCT FROM OLD.profile_fingerprint
           OR NEW.profile_verified_at IS DISTINCT FROM OLD.profile_verified_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_config_mutation_forbidden';
        END IF;

        -- The UPDATE statement already owns the endpoint row lock before a
        -- row trigger starts.  Taking account/run locks here would invert the
        -- canonical account -> endpoint order used by feed-intent writers and
        -- can deadlock.  These are deliberately validation-only reads; the
        -- production promotion API must lock account -> endpoint -> run before
        -- issuing this guarded UPDATE.
        SELECT * INTO artifact_row
          FROM marketplaces_marketplacefeedartifact
         WHERE id = NEW.current_artifact_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;

        SELECT * INTO run_row
          FROM marketplaces_marketplacefeedrun
         WHERE id = artifact_row.run_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;

        SELECT * INTO owner_row
          FROM marketplaces_marketplaceaccount
         WHERE id = OLD.account_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;

        SELECT * INTO tenant_row
          FROM tenants_tenant
         WHERE id = owner_row.tenant_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;

        IF owner_row.deleted_at IS NOT NULL
           OR owner_row.is_active IS NOT TRUE
           OR tenant_row.is_active IS NOT TRUE
           OR artifact_row.endpoint_id IS DISTINCT FROM OLD.public_id
           OR artifact_row.account_id IS DISTINCT FROM OLD.account_id
           OR run_row.id IS DISTINCT FROM artifact_row.run_id
           OR run_row.account_id IS DISTINCT FROM OLD.account_id
           OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
           OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
           OR run_row.account_identity_digest IS DISTINCT FROM OLD.owner_identity_digest
           OR run_row.feed_artifact_id IS DISTINCT FROM artifact_row.id
           OR owner_row.feed_intent_revision IS DISTINCT FROM OLD.source_intent_revision
           OR run_row.source_intent_revision IS DISTINCT FROM OLD.source_intent_revision
           OR run_row.source_intent_revision IS DISTINCT FROM NEW.source_intent_revision
           OR run_row.endpoint_revision IS DISTINCT FROM OLD.artifact_revision
           OR run_row.predecessor_artifact_id IS DISTINCT FROM OLD.current_artifact_id
           OR run_row.artifact_upload_attempt IS DISTINCT FROM artifact_row.upload_attempt
           OR run_row.payload_sha256 IS NULL
           OR run_row.payload_sha256 = ''
           OR run_row.payload_sha256 IS DISTINCT FROM artifact_row.payload_sha256
           OR NEW.artifact_revision IS DISTINCT FROM OLD.artifact_revision + 1
           OR NEW.artifact_promoted_at IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_promotion_rejected';
        END IF;
    ELSE
        IF (
               NEW.storage_mode IS DISTINCT FROM OLD.storage_mode
               AND NOT (
                   OLD.storage_mode = 'private_generation'
                   AND NEW.storage_mode = 'legacy_bridge'
                   AND (
                       (
                           OLD.serve_enabled IS TRUE
                           AND NEW.serve_enabled IS TRUE
                       )
                       OR (
                           OLD.serve_enabled IS FALSE
                           AND NEW.serve_enabled IS FALSE
                           AND OLD.current_artifact_id IS NULL
                           AND OLD.artifact_revision = 0
                           AND OLD.artifact_promoted_at IS NULL
                       )
                   )
               )
           )
           OR NEW.artifact_revision IS DISTINCT FROM OLD.artifact_revision
           OR NEW.artifact_promoted_at IS DISTINCT FROM OLD.artifact_promoted_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_endpoint_artifact_metadata_rejected';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_endpoint_art_guard_trg
    ON marketplaces_marketplacefeedendpoint;
CREATE TRIGGER mkt_feed_endpoint_art_guard_trg
BEFORE UPDATE ON marketplaces_marketplacefeedendpoint
FOR EACH ROW EXECUTE FUNCTION mkt_feed_endpoint_art_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_run_art_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    artifact_exists boolean;
BEGIN
    IF OLD.feed_artifact_id IS DISTINCT FROM NEW.feed_artifact_id THEN
        IF OLD.feed_artifact_id IS NOT NULL
           OR NEW.feed_artifact_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_mutation_forbidden';
        END IF;

        -- The run row is already locked by its UPDATE.  The artifact is
        -- immutable and protected by an FK, so a plain validation read avoids
        -- a run -> artifact lock inversion with endpoint promotion.
        SELECT * INTO artifact_row
          FROM marketplaces_marketplacefeedartifact
         WHERE id = NEW.feed_artifact_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_selection_rejected';
        END IF;

        IF artifact_row.run_id IS DISTINCT FROM OLD.id
           OR artifact_row.account_id IS DISTINCT FROM NEW.account_id
           OR artifact_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR artifact_row.upload_attempt IS DISTINCT FROM NEW.artifact_upload_attempt THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_selection_rejected';
        END IF;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifact
         WHERE run_id = OLD.id
    ) INTO artifact_exists;

    IF (NEW.feed_artifact_id IS NOT NULL OR artifact_exists)
       AND (
           NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.marketplace IS DISTINCT FROM OLD.marketplace
           OR NEW.account_identity_digest IS DISTINCT FROM OLD.account_identity_digest
           OR NEW.source_intent_revision IS DISTINCT FROM OLD.source_intent_revision
           OR NEW.endpoint_revision IS DISTINCT FROM OLD.endpoint_revision
           OR NEW.predecessor_artifact_id IS DISTINCT FROM OLD.predecessor_artifact_id
           OR NEW.artifact_upload_attempt IS DISTINCT FROM OLD.artifact_upload_attempt
           OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_run_artifact_snapshot_mutation_forbidden';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_run_art_guard_trg
    ON marketplaces_marketplacefeedrun;
CREATE TRIGGER mkt_feed_run_art_guard_trg
BEFORE UPDATE ON marketplaces_marketplacefeedrun
FOR EACH ROW EXECUTE FUNCTION mkt_feed_run_art_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_fetch_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    endpoint_row marketplaces_marketplacefeedendpoint%ROWTYPE;
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
    owner_id bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_update_forbidden';
    END IF;

    -- Resolve the owner without a row lock, then acquire the same canonical
    -- account -> endpoint -> artifact -> run order as artifact writers.  The
    -- locked endpoint is read again below, so a concurrent owner change is
    -- rejected rather than trusted from this preliminary lookup. Tenant state
    -- is a validation snapshot rather than a trailing row lock: taking tenant
    -- after account would invert tenant lifecycle writers.
    SELECT account_id INTO owner_id
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    SELECT * INTO owner_row
      FROM marketplaces_marketplaceaccount
     WHERE id = owner_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    SELECT * INTO endpoint_row
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    SELECT * INTO artifact_row
      FROM marketplaces_marketplacefeedartifact
     WHERE id = NEW.artifact_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = artifact_row.run_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    SELECT * INTO tenant_row
      FROM tenants_tenant
     WHERE id = owner_row.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    IF owner_row.deleted_at IS NOT NULL
       OR owner_row.is_active IS NOT TRUE
       OR tenant_row.is_active IS NOT TRUE
       OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
       OR artifact_row.endpoint_id IS DISTINCT FROM endpoint_row.public_id
       OR artifact_row.account_id IS DISTINCT FROM endpoint_row.account_id
       OR endpoint_row.current_artifact_id IS DISTINCT FROM artifact_row.id
       OR run_row.id IS DISTINCT FROM artifact_row.run_id
       OR run_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
       OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
       OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
       OR run_row.feed_artifact_id IS DISTINCT FROM artifact_row.id
       OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.endpoint_revision + 1 IS DISTINCT FROM endpoint_row.artifact_revision
       OR run_row.artifact_upload_attempt IS DISTINCT FROM artifact_row.upload_attempt
       OR run_row.payload_sha256 IS DISTINCT FROM artifact_row.payload_sha256
       OR NEW.capability_revision IS DISTINCT FROM endpoint_row.capability_revision
       OR NEW.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
       OR NEW.source_intent_revision IS DISTINCT FROM run_row.source_intent_revision
       OR NEW.run_revision IS DISTINCT FROM run_row.revision
       OR (
           NEW.accepted_token_key_id IS DISTINCT FROM endpoint_row.token_key_id
           AND NEW.accepted_token_key_id IS DISTINCT FROM endpoint_row.previous_token_key_id
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_fetch_evidence_rejected';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_fetch_guard_trg
    ON marketplaces_marketplacefeedfetchevidence;
CREATE TRIGGER mkt_feed_fetch_guard_trg
BEFORE INSERT OR UPDATE ON marketplaces_marketplacefeedfetchevidence
FOR EACH ROW EXECUTE FUNCTION mkt_feed_fetch_guard_fn();


DO $$
BEGIN
    -- There is no safe way to infer an exact, pre-PUT ledger snapshot for an
    -- already-existing Artifact/pointer. Production is still dark, so abort
    -- loudly instead of silently grandfathering unverifiable generations.
    -- Acquire the first incompatible write-table lock used by an attachment
    -- before the ledger. Its canonical parent/ledger SELECT FOR UPDATE reads
    -- hold ROW SHARE table locks, which do not conflict with SHARE ROW
    -- EXCLUSIVE. Its later Artifact -> Run -> UploadAttempt writes do conflict;
    -- taking the ledger first could therefore deadlock after Artifact INSERT.
    LOCK TABLE
        marketplaces_marketplacefeedartifact,
        marketplaces_marketplacefeedrun,
        marketplaces_marketplacefeedartifactuploadattempt,
        marketplaces_marketplacefeedendpoint
    IN SHARE ROW EXCLUSIVE MODE;

    IF EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM marketplaces_marketplacefeedartifact LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE feed_artifact_id IS NOT NULL
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE artifact_upload_attempt <> 0
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedendpoint
         WHERE current_artifact_id IS NOT NULL
         LIMIT 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_ledger_preflight_failed';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION mkt_feed_upload_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    endpoint_row marketplaces_marketplacefeedendpoint%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
    prior_attempt_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    expected_object_key text;
    last_attempt integer;
    unresolved_attempt_exists boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_delete_forbidden';
    END IF;

    IF TG_OP = 'INSERT' THEN
        SELECT * INTO owner_row
          FROM marketplaces_marketplaceaccount
         WHERE id = NEW.account_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_insert_rejected';
        END IF;

        SELECT * INTO endpoint_row
          FROM marketplaces_marketplacefeedendpoint
         WHERE public_id = NEW.endpoint_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_insert_rejected';
        END IF;

        SELECT * INTO run_row
          FROM marketplaces_marketplacefeedrun
         WHERE id = NEW.run_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_insert_rejected';
        END IF;

        SELECT * INTO tenant_row
          FROM tenants_tenant
         WHERE id = owner_row.tenant_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_insert_rejected';
        END IF;

        expected_object_key := format(
            'private-feeds/v1/%s/%s/%s/feed.xml',
            NEW.endpoint_id::text,
            NEW.run_id::text,
            lpad(NEW.attempt_no::text, 5, '0')
        );

        SELECT COALESCE(MAX(attempt_no), 0),
               COALESCE(
                   bool_or(state IS DISTINCT FROM 'no_object'),
                   false
               )
          INTO last_attempt, unresolved_attempt_exists
         FROM marketplaces_marketplacefeedartifactuploadattempt
         WHERE run_id = NEW.run_id;

        IF last_attempt > 0 THEN
            SELECT * INTO prior_attempt_row
              FROM marketplaces_marketplacefeedartifactuploadattempt
             WHERE run_id = NEW.run_id
               AND attempt_no = last_attempt;
        END IF;

        IF NEW.state IS DISTINCT FROM 'prepared'
           OR NEW.revision IS DISTINCT FROM 0
           OR NEW.object_key IS DISTINCT FROM expected_object_key
           OR NEW.attempt_no IS DISTINCT FROM last_attempt + 1
           OR unresolved_attempt_exists
           OR owner_row.deleted_at IS NOT NULL
           OR owner_row.is_active IS NOT TRUE
           OR tenant_row.is_active IS NOT TRUE
           OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
           OR NOT (
               (endpoint_row.storage_mode = 'private_generation'
                AND endpoint_row.serve_enabled IS FALSE)
               OR (endpoint_row.storage_mode = 'legacy_bridge'
                   AND endpoint_row.serve_enabled IS TRUE)
           )
           OR run_row.account_id IS DISTINCT FROM owner_row.id
           OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
           OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
           OR run_row.state IS DISTINCT FROM 'preparing'
           OR run_row.claim_token IS NULL
           OR run_row.claimed_until IS NULL
           OR run_row.claimed_until <= clock_timestamp()
           OR run_row.submitted_at IS NOT NULL
           OR run_row.provider_run_id IS NOT NULL
           OR run_row.provider_predecessor_run_id IS NOT NULL
           OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
           OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
           OR run_row.source_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
           OR run_row.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
           OR run_row.predecessor_artifact_id IS DISTINCT FROM endpoint_row.current_artifact_id
           OR run_row.feed_artifact_id IS NOT NULL
           OR run_row.artifact_upload_attempt IS DISTINCT FROM 0
           OR run_row.payload_sha256 IS NULL
           OR run_row.payload_sha256 = ''
           OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR (
               last_attempt > 0
               AND (
                   prior_attempt_row.account_id IS DISTINCT FROM NEW.account_id
                   OR prior_attempt_row.endpoint_id IS DISTINCT FROM NEW.endpoint_id
                   OR prior_attempt_row.storage_bucket IS DISTINCT FROM NEW.storage_bucket
                   OR prior_attempt_row.expected_bucket_owner IS DISTINCT FROM NEW.expected_bucket_owner
                   OR prior_attempt_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
                   OR prior_attempt_row.size_bytes IS DISTINCT FROM NEW.size_bytes
                   OR prior_attempt_row.projection_count IS DISTINCT FROM NEW.projection_count
                   OR prior_attempt_row.content_type IS DISTINCT FROM NEW.content_type
               )
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_insert_rejected';
        END IF;

        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.account_id IS DISTINCT FROM OLD.account_id
       OR NEW.endpoint_id IS DISTINCT FROM OLD.endpoint_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
       OR NEW.storage_bucket IS DISTINCT FROM OLD.storage_bucket
       OR NEW.expected_bucket_owner IS DISTINCT FROM OLD.expected_bucket_owner
       OR NEW.object_key IS DISTINCT FROM OLD.object_key
       OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
       OR NEW.projection_count IS DISTINCT FROM OLD.projection_count
       OR NEW.content_type IS DISTINCT FROM OLD.content_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision IS DISTINCT FROM OLD.revision + 1
       OR NEW.state IS NOT DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_update_rejected';
    END IF;

    IF OLD.state IN ('attached', 'no_object', 'orphaned', 'manual_review') THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_terminal_mutation_forbidden';
    END IF;

    IF NOT (
        (OLD.state = 'prepared' AND NEW.state IN ('put_pending', 'no_object'))
        OR (
            OLD.state = 'put_pending'
            AND NEW.state IN ('version_known', 'no_object', 'manual_review')
        )
        OR (
            OLD.state = 'version_known'
            AND NEW.state IN ('verified', 'orphaned', 'manual_review')
        )
        OR (
            OLD.state = 'verified'
            AND NEW.state IN ('attached', 'orphaned', 'manual_review')
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_transition_rejected';
    END IF;

    -- Once an external boundary value is known it can never be rewritten or
    -- cleared. The only nullable-to-known transitions are the corresponding
    -- forward states (or MANUAL_REVIEW preserving an exact returned version).
    IF OLD.put_run_revision IS NOT NULL
       AND NEW.put_run_revision IS DISTINCT FROM OLD.put_run_revision THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_put_snapshot_mutation_forbidden';
    END IF;
    IF OLD.put_started_at IS NOT NULL
       AND NEW.put_started_at IS DISTINCT FROM OLD.put_started_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_put_snapshot_mutation_forbidden';
    END IF;
    IF OLD.object_version_id IS NOT NULL
       AND NEW.object_version_id IS DISTINCT FROM OLD.object_version_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_version_mutation_forbidden';
    END IF;
    IF OLD.version_known_at IS NOT NULL
       AND NEW.version_known_at IS DISTINCT FROM OLD.version_known_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_version_mutation_forbidden';
    END IF;
    IF OLD.verified_at IS NOT NULL
       AND NEW.verified_at IS DISTINCT FROM OLD.verified_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_verification_mutation_forbidden';
    END IF;

    IF OLD.state = 'prepared' AND NEW.state = 'put_pending' THEN
        IF NEW.put_run_revision IS NULL
           OR NEW.put_started_at IS NULL
           OR NEW.object_version_id IS NOT NULL
           OR NEW.version_known_at IS NOT NULL
           OR NEW.verified_at IS NOT NULL
           OR NEW.attached_at IS NOT NULL
           OR NEW.resolved_at IS NOT NULL
           OR NEW.safe_error_code IS DISTINCT FROM '' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_put_boundary_rejected';
        END IF;
    ELSIF OLD.state = 'prepared' AND NEW.state = 'no_object' THEN
        IF NEW.put_run_revision IS NOT NULL
           OR NEW.put_started_at IS NOT NULL
           OR NEW.object_version_id IS NOT NULL
           OR NEW.version_known_at IS NOT NULL
           OR NEW.verified_at IS NOT NULL
           OR NEW.attached_at IS NOT NULL
           OR NEW.resolved_at IS NULL
           OR NEW.safe_error_code IS NULL
           OR NEW.safe_error_code = '' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_abandon_rejected';
        END IF;
    ELSIF OLD.state = 'put_pending' THEN
        IF NEW.put_run_revision IS DISTINCT FROM OLD.put_run_revision
           OR NEW.put_started_at IS DISTINCT FROM OLD.put_started_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_put_snapshot_mutation_forbidden';
        END IF;
        IF NEW.state = 'version_known' AND (
            NEW.object_version_id IS NULL
            OR NEW.version_known_at IS NULL
            OR NEW.verified_at IS NOT NULL
            OR NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NOT NULL
            OR NEW.safe_error_code IS DISTINCT FROM ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_version_boundary_rejected';
        ELSIF NEW.state = 'no_object' AND (
            NEW.object_version_id IS NOT NULL
            OR NEW.version_known_at IS NOT NULL
            OR NEW.verified_at IS NOT NULL
            OR NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NULL
            OR NEW.safe_error_code IS NULL
            OR NEW.safe_error_code = ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_no_object_rejected';
        ELSIF NEW.state = 'manual_review' AND (
            NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NULL
            OR NEW.safe_error_code IS NULL
            OR NEW.safe_error_code = ''
            OR (
                (NEW.object_version_id IS NULL)
                IS DISTINCT FROM (NEW.version_known_at IS NULL)
            )
            OR NEW.verified_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_manual_review_rejected';
        END IF;
    ELSIF OLD.state = 'version_known' THEN
        IF NEW.put_run_revision IS DISTINCT FROM OLD.put_run_revision
           OR NEW.put_started_at IS DISTINCT FROM OLD.put_started_at
           OR NEW.object_version_id IS DISTINCT FROM OLD.object_version_id
           OR NEW.version_known_at IS DISTINCT FROM OLD.version_known_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_version_mutation_forbidden';
        END IF;
        IF NEW.state = 'verified' AND (
            NEW.verified_at IS NULL
            OR NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NOT NULL
            OR NEW.safe_error_code IS DISTINCT FROM ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_verification_rejected';
        ELSIF NEW.state IN ('orphaned', 'manual_review') AND (
            NEW.verified_at IS NOT NULL
            OR NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NULL
            OR NEW.safe_error_code IS NULL
            OR NEW.safe_error_code = ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_resolution_rejected';
        END IF;
    ELSIF OLD.state = 'verified' THEN
        IF NEW.put_run_revision IS DISTINCT FROM OLD.put_run_revision
           OR NEW.put_started_at IS DISTINCT FROM OLD.put_started_at
           OR NEW.object_version_id IS DISTINCT FROM OLD.object_version_id
           OR NEW.version_known_at IS DISTINCT FROM OLD.version_known_at
           OR NEW.verified_at IS DISTINCT FROM OLD.verified_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_verification_mutation_forbidden';
        END IF;
        IF NEW.state = 'attached' AND (
            NEW.attached_at IS NULL
            OR NEW.resolved_at IS NULL
            OR NEW.safe_error_code IS DISTINCT FROM ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_attachment_rejected';
        ELSIF NEW.state IN ('orphaned', 'manual_review') AND (
            NEW.attached_at IS NOT NULL
            OR NEW.resolved_at IS NULL
            OR NEW.safe_error_code IS NULL
            OR NEW.safe_error_code = ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_resolution_rejected';
        END IF;
    END IF;

    -- Updates execute after the application has acquired the canonical parent
    -- locks. These are validation-only reads to avoid a ledger -> parent lock
    -- inversion.
    SELECT * INTO owner_row
      FROM marketplaces_marketplaceaccount
     WHERE id = NEW.account_id;
    SELECT * INTO endpoint_row
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id;
    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = NEW.run_id;
    SELECT * INTO tenant_row
      FROM tenants_tenant
     WHERE id = owner_row.tenant_id;
    IF owner_row.id IS NULL
       OR endpoint_row.public_id IS NULL
       OR run_row.id IS NULL
       OR tenant_row.id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_snapshot_rejected';
    END IF;

    -- A successful PUT is an external fact, not a freshness decision.  Once
    -- the exact VersionId has been returned, preserve it even when the claim
    -- expired, a new worker owns the run, or the owner/current endpoint became
    -- unavailable while the request was in flight.  Static run fields are
    -- sealed as soon as a ledger row exists; these checks bind the capture to
    -- that immutable row without consulting mutable availability/freshness.
    IF OLD.state = 'put_pending' AND NEW.state = 'version_known' THEN
        IF endpoint_row.account_id IS DISTINCT FROM NEW.account_id
           OR run_row.account_id IS DISTINCT FROM NEW.account_id
           OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR run_row.revision < NEW.put_run_revision THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_version_snapshot_rejected';
        END IF;
    ELSIF NEW.state IN ('put_pending', 'verified') THEN
        IF owner_row.deleted_at IS NOT NULL
           OR owner_row.is_active IS NOT TRUE
           OR tenant_row.is_active IS NOT TRUE
           OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
           OR NOT (
               (endpoint_row.storage_mode = 'private_generation'
                AND endpoint_row.serve_enabled IS FALSE)
               OR (endpoint_row.storage_mode = 'legacy_bridge'
                   AND endpoint_row.serve_enabled IS TRUE)
           )
           OR run_row.account_id IS DISTINCT FROM owner_row.id
           OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
           OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
           OR run_row.state IS DISTINCT FROM 'preparing'
           OR run_row.claim_token IS NULL
           OR run_row.claimed_until IS NULL
           OR run_row.claimed_until <= clock_timestamp()
           OR run_row.submitted_at IS NOT NULL
           OR run_row.provider_run_id IS NOT NULL
           OR run_row.provider_predecessor_run_id IS NOT NULL
           OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
           OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
           OR run_row.source_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
           OR run_row.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
           OR run_row.predecessor_artifact_id IS DISTINCT FROM endpoint_row.current_artifact_id
           OR run_row.feed_artifact_id IS NOT NULL
           OR run_row.artifact_upload_attempt IS DISTINCT FROM 0
           OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR run_row.revision < NEW.put_run_revision
           OR (
               NEW.state = 'put_pending'
               AND run_row.revision IS DISTINCT FROM NEW.put_run_revision
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_snapshot_rejected';
        END IF;
    ELSIF NEW.state = 'attached' THEN
        SELECT * INTO artifact_row
          FROM marketplaces_marketplacefeedartifact
         WHERE run_id = NEW.run_id
           AND upload_attempt = NEW.attempt_no;
        IF NOT FOUND
           OR owner_row.deleted_at IS NOT NULL
           OR owner_row.is_active IS NOT TRUE
           OR tenant_row.is_active IS NOT TRUE
           OR run_row.state IS DISTINCT FROM 'preparing'
           OR run_row.claim_token IS NULL
           OR run_row.claimed_until IS NULL
           OR run_row.claimed_until <= clock_timestamp()
           OR run_row.submitted_at IS NOT NULL
           OR run_row.provider_run_id IS NOT NULL
           OR run_row.provider_predecessor_run_id IS NOT NULL
           OR run_row.revision < NEW.put_run_revision
           OR run_row.feed_artifact_id IS DISTINCT FROM artifact_row.id
           OR run_row.artifact_upload_attempt IS DISTINCT FROM NEW.attempt_no
           OR artifact_row.account_id IS DISTINCT FROM NEW.account_id
           OR artifact_row.endpoint_id IS DISTINCT FROM NEW.endpoint_id
           OR artifact_row.storage_bucket IS DISTINCT FROM NEW.storage_bucket
           OR artifact_row.object_key IS DISTINCT FROM NEW.object_key
           OR artifact_row.object_version_id IS DISTINCT FROM NEW.object_version_id
           OR artifact_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR artifact_row.size_bytes IS DISTINCT FROM NEW.size_bytes
           OR artifact_row.listing_count IS DISTINCT FROM NEW.projection_count
           OR artifact_row.content_type IS DISTINCT FROM NEW.content_type
           OR artifact_row.verified_at IS DISTINCT FROM NEW.verified_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_upload_attachment_rejected';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_upload_guard_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
CREATE TRIGGER mkt_feed_upload_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON marketplaces_marketplacefeedartifactuploadattempt
FOR EACH ROW EXECUTE FUNCTION mkt_feed_upload_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_artifact_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    endpoint_row marketplaces_marketplacefeedendpoint%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    ledger_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
    expected_object_key text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_update_forbidden';
    END IF;

    SELECT * INTO owner_row
      FROM marketplaces_marketplaceaccount
     WHERE id = NEW.account_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO endpoint_row
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = NEW.run_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO ledger_row
      FROM marketplaces_marketplacefeedartifactuploadattempt
     WHERE run_id = NEW.run_id
       AND attempt_no = NEW.upload_attempt
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    SELECT * INTO tenant_row
      FROM tenants_tenant
     WHERE id = owner_row.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    expected_object_key := format(
        'private-feeds/v1/%s/%s/%s/feed.xml',
        NEW.endpoint_id::text,
        NEW.run_id::text,
        lpad(NEW.upload_attempt::text, 5, '0')
    );

    IF owner_row.deleted_at IS NOT NULL
       OR owner_row.is_active IS NOT TRUE
       OR tenant_row.is_active IS NOT TRUE
       OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
       OR NOT (
           (endpoint_row.storage_mode = 'private_generation'
            AND endpoint_row.serve_enabled IS FALSE)
           OR (endpoint_row.storage_mode = 'legacy_bridge'
               AND endpoint_row.serve_enabled IS TRUE)
       )
       OR run_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
       OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
       OR run_row.state IS DISTINCT FROM 'preparing'
       OR run_row.claim_token IS NULL
       OR run_row.claimed_until IS NULL
       OR run_row.claimed_until <= clock_timestamp()
       OR run_row.submitted_at IS NOT NULL
       OR run_row.provider_run_id IS NOT NULL
       OR run_row.provider_predecessor_run_id IS NOT NULL
       OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
       OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.source_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
       OR run_row.predecessor_artifact_id IS DISTINCT FROM endpoint_row.current_artifact_id
       OR run_row.feed_artifact_id IS NOT NULL
       OR run_row.artifact_upload_attempt IS DISTINCT FROM 0
       OR run_row.payload_sha256 IS NULL
       OR run_row.payload_sha256 = ''
       OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
       OR ledger_row.state IS DISTINCT FROM 'verified'
       OR ledger_row.account_id IS DISTINCT FROM NEW.account_id
       OR ledger_row.endpoint_id IS DISTINCT FROM NEW.endpoint_id
       OR run_row.revision < ledger_row.put_run_revision
       OR ledger_row.storage_bucket IS DISTINCT FROM NEW.storage_bucket
       OR ledger_row.object_key IS DISTINCT FROM NEW.object_key
       OR ledger_row.object_version_id IS DISTINCT FROM NEW.object_version_id
       OR ledger_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
       OR ledger_row.size_bytes IS DISTINCT FROM NEW.size_bytes
       OR ledger_row.projection_count IS DISTINCT FROM NEW.listing_count
       OR ledger_row.content_type IS DISTINCT FROM NEW.content_type
       OR ledger_row.verified_at IS DISTINCT FROM NEW.verified_at
       OR NEW.verification_method IS DISTINCT FROM 'version_readback_sha256'
       OR NEW.object_key IS DISTINCT FROM expected_object_key THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_artifact_guard_trg
    ON marketplaces_marketplacefeedartifact;
CREATE TRIGGER mkt_feed_artifact_guard_trg
BEFORE INSERT OR UPDATE ON marketplaces_marketplacefeedartifact
FOR EACH ROW EXECUTE FUNCTION mkt_feed_artifact_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_run_art_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    ledger_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    artifact_exists boolean;
    ledger_exists boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.feed_artifact_id IS NOT NULL
           OR NEW.artifact_upload_attempt IS DISTINCT FROM 0 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_initial_state_rejected';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.feed_artifact_id IS DISTINCT FROM NEW.feed_artifact_id THEN
        IF OLD.feed_artifact_id IS NOT NULL
           OR NEW.feed_artifact_id IS NULL
           OR OLD.artifact_upload_attempt IS DISTINCT FROM 0 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_mutation_forbidden';
        END IF;

        SELECT * INTO artifact_row
          FROM marketplaces_marketplacefeedartifact
         WHERE id = NEW.feed_artifact_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_selection_rejected';
        END IF;

        SELECT * INTO ledger_row
          FROM marketplaces_marketplacefeedartifactuploadattempt
         WHERE run_id = OLD.id
           AND attempt_no = artifact_row.upload_attempt;
        IF NOT FOUND
           OR ledger_row.state IS DISTINCT FROM 'verified'
           OR OLD.state IS DISTINCT FROM 'preparing'
           OR NEW.state IS DISTINCT FROM 'preparing'
           OR OLD.revision < ledger_row.put_run_revision
           OR NEW.revision IS DISTINCT FROM OLD.revision
           OR NEW.claim_token IS NULL
           OR NEW.claimed_until IS NULL
           OR NEW.claimed_until <= clock_timestamp()
           OR NEW.submitted_at IS NOT NULL
           OR NEW.provider_run_id IS NOT NULL
           OR NEW.provider_predecessor_run_id IS NOT NULL
           OR artifact_row.run_id IS DISTINCT FROM OLD.id
           OR artifact_row.account_id IS DISTINCT FROM NEW.account_id
           OR artifact_row.endpoint_id IS DISTINCT FROM ledger_row.endpoint_id
           OR artifact_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR artifact_row.upload_attempt IS DISTINCT FROM NEW.artifact_upload_attempt
           OR ledger_row.account_id IS DISTINCT FROM NEW.account_id
           OR ledger_row.storage_bucket IS DISTINCT FROM artifact_row.storage_bucket
           OR ledger_row.object_key IS DISTINCT FROM artifact_row.object_key
           OR ledger_row.object_version_id IS DISTINCT FROM artifact_row.object_version_id
           OR ledger_row.payload_sha256 IS DISTINCT FROM artifact_row.payload_sha256
           OR ledger_row.size_bytes IS DISTINCT FROM artifact_row.size_bytes
           OR ledger_row.projection_count IS DISTINCT FROM artifact_row.listing_count
           OR ledger_row.content_type IS DISTINCT FROM artifact_row.content_type
           OR ledger_row.verified_at IS DISTINCT FROM artifact_row.verified_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_selection_rejected';
        END IF;
    ELSIF NEW.artifact_upload_attempt IS DISTINCT FROM OLD.artifact_upload_attempt THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_run_artifact_attempt_rejected';
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifact
         WHERE run_id = OLD.id
    ) INTO artifact_exists;

    SELECT EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         WHERE run_id = OLD.id
    ) INTO ledger_exists;

    IF (NEW.feed_artifact_id IS NOT NULL OR artifact_exists OR ledger_exists)
       AND (
           NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.marketplace IS DISTINCT FROM OLD.marketplace
           OR NEW.account_identity_digest IS DISTINCT FROM OLD.account_identity_digest
           OR NEW.source_intent_revision IS DISTINCT FROM OLD.source_intent_revision
           OR NEW.endpoint_revision IS DISTINCT FROM OLD.endpoint_revision
           OR NEW.predecessor_artifact_id IS DISTINCT FROM OLD.predecessor_artifact_id
           OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_run_artifact_snapshot_mutation_forbidden';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_run_art_guard_trg
    ON marketplaces_marketplacefeedrun;
CREATE TRIGGER mkt_feed_run_art_guard_trg
BEFORE INSERT OR UPDATE ON marketplaces_marketplacefeedrun
FOR EACH ROW EXECUTE FUNCTION mkt_feed_run_art_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_artifact_attach_deferred_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    ledger_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    artifact_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'marketplaces_marketplacefeedartifact' THEN
        artifact_id := NEW.id;
    ELSIF TG_TABLE_NAME = 'marketplaces_marketplacefeedrun' THEN
        artifact_id := NEW.feed_artifact_id;
    ELSE
        SELECT id INTO artifact_id
          FROM marketplaces_marketplacefeedartifact
         WHERE run_id = NEW.run_id
           AND upload_attempt = NEW.attempt_no;
    END IF;

    SELECT * INTO artifact_row
      FROM marketplaces_marketplacefeedartifact
     WHERE id = artifact_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_attach_commit_rejected';
    END IF;

    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = artifact_row.run_id;
    SELECT * INTO ledger_row
      FROM marketplaces_marketplacefeedartifactuploadattempt
     WHERE run_id = artifact_row.run_id
       AND attempt_no = artifact_row.upload_attempt;

    IF run_row.id IS NULL
       OR ledger_row.id IS NULL
       OR run_row.feed_artifact_id IS DISTINCT FROM artifact_row.id
       OR run_row.artifact_upload_attempt IS DISTINCT FROM artifact_row.upload_attempt
       OR ledger_row.state IS DISTINCT FROM 'attached'
       OR ledger_row.account_id IS DISTINCT FROM artifact_row.account_id
       OR ledger_row.endpoint_id IS DISTINCT FROM artifact_row.endpoint_id
       OR ledger_row.storage_bucket IS DISTINCT FROM artifact_row.storage_bucket
       OR ledger_row.object_key IS DISTINCT FROM artifact_row.object_key
       OR ledger_row.object_version_id IS DISTINCT FROM artifact_row.object_version_id
       OR ledger_row.payload_sha256 IS DISTINCT FROM artifact_row.payload_sha256
       OR ledger_row.size_bytes IS DISTINCT FROM artifact_row.size_bytes
       OR ledger_row.projection_count IS DISTINCT FROM artifact_row.listing_count
       OR ledger_row.content_type IS DISTINCT FROM artifact_row.content_type
       OR ledger_row.verified_at IS DISTINCT FROM artifact_row.verified_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_attach_commit_rejected';
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_artifact_attach_deferred_trg
    ON marketplaces_marketplacefeedartifact;
CREATE CONSTRAINT TRIGGER mkt_feed_artifact_attach_deferred_trg
AFTER INSERT ON marketplaces_marketplacefeedartifact
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION mkt_feed_artifact_attach_deferred_fn();

DROP TRIGGER IF EXISTS mkt_feed_run_attach_deferred_trg
    ON marketplaces_marketplacefeedrun;
CREATE CONSTRAINT TRIGGER mkt_feed_run_attach_deferred_trg
AFTER UPDATE ON marketplaces_marketplacefeedrun
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (
    OLD.feed_artifact_id IS DISTINCT FROM NEW.feed_artifact_id
    AND NEW.feed_artifact_id IS NOT NULL
)
EXECUTE FUNCTION mkt_feed_artifact_attach_deferred_fn();

DROP TRIGGER IF EXISTS mkt_feed_upload_attach_deferred_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
CREATE CONSTRAINT TRIGGER mkt_feed_upload_attach_deferred_trg
AFTER UPDATE ON marketplaces_marketplacefeedartifactuploadattempt
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state = 'attached')
EXECUTE FUNCTION mkt_feed_artifact_attach_deferred_fn();


DO $$
BEGIN
    -- 0034 and 0035 commit independently.  Repeat the fail-closed dark
    -- preflight so a writer in that deployment gap cannot create provenance
    -- that the new source/audit contract would have to guess.
    --
    -- First incompatible writes are Artifact (attach), Audit (operator
    -- reconciliation), and UploadAttempt (direct PUT response), in that
    -- order.  In particular, UploadAttempt -> Audit would deadlock with the
    -- operator's Audit INSERT -> UploadAttempt UPDATE sequence.
    LOCK TABLE
        marketplaces_marketplacefeedartifact,
        marketplaces_marketplacefeedputreconciliationaudit,
        marketplaces_marketplacefeedartifactuploadattempt,
        marketplaces_marketplacefeedrun,
        marketplaces_marketplacefeedendpoint
    IN SHARE ROW EXCLUSIVE MODE;

    IF EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedputreconciliationaudit
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         WHERE put_resolution_source <> ''
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM marketplaces_marketplacefeedartifact LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE feed_artifact_id IS NOT NULL
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE artifact_upload_attempt <> 0
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedendpoint
         WHERE current_artifact_id IS NOT NULL
         LIMIT 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_put_audit_guard_preflight_failed';
    END IF;
END;
$$;


CREATE OR REPLACE FUNCTION mkt_feed_put_audit_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    attempt_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    observed_at timestamp with time zone := clock_timestamp();
BEGIN
    IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_put_audit_immutable';
    END IF;

    -- Validation-only read: taking a row lock here would invert the canonical
    -- Attempt -> Audit application row-lock order.  The FK may wait for a
    -- concurrent Attempt writer, but neither direct capture nor this guard
    -- waits on an uncommitted Audit row; the deferred pair rejects stale data.
    SELECT * INTO attempt_row
      FROM marketplaces_marketplacefeedartifactuploadattempt
     WHERE id = NEW.attempt_id;
    IF NOT FOUND
       OR attempt_row.state IS DISTINCT FROM 'put_pending'
       OR attempt_row.put_resolution_source IS DISTINCT FROM ''
       OR attempt_row.revision IS DISTINCT FROM NEW.pre_revision
       OR NEW.post_revision IS DISTINCT FROM NEW.pre_revision + 1
       OR NEW.from_state IS DISTINCT FROM 'put_pending'
       OR attempt_row.put_started_at IS NULL
       OR NEW.origin_process_terminated_at < attempt_row.put_started_at
       OR NEW.settlement_window_seconds IS DISTINCT FROM 900
       OR observed_at < (
           NEW.origin_process_terminated_at + interval '900 seconds'
       )
       OR NEW.reconciliation_started_at > observed_at
       OR NEW.decision_at > observed_at
       OR attempt_row.object_version_id IS NOT NULL
       OR attempt_row.version_known_at IS NOT NULL
       OR attempt_row.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_put_audit_insert_rejected';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_put_audit_guard_trg
    ON marketplaces_marketplacefeedputreconciliationaudit;
CREATE TRIGGER mkt_feed_put_audit_guard_trg
BEFORE INSERT OR UPDATE OR DELETE
ON marketplaces_marketplacefeedputreconciliationaudit
FOR EACH ROW EXECUTE FUNCTION mkt_feed_put_audit_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_put_resolution_source_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    audit_row marketplaces_marketplacefeedputreconciliationaudit%ROWTYPE;
    audit_exists boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.put_resolution_source IS DISTINCT FROM '' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_put_resolution_source_insert_rejected';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.put_resolution_source IS DISTINCT FROM '' THEN
        IF NEW.put_resolution_source IS DISTINCT FROM OLD.put_resolution_source THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_put_resolution_source_immutable';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.put_resolution_source IS NOT DISTINCT FROM '' THEN
        IF OLD.state = 'put_pending'
           AND NEW.state IN ('no_object', 'version_known', 'manual_review') THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_put_resolution_source_required';
        END IF;
        RETURN NEW;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedputreconciliationaudit
         WHERE attempt_id = NEW.id
    ) INTO audit_exists;

    IF NEW.put_resolution_source = 'put_response' THEN
        IF OLD.state IS DISTINCT FROM 'put_pending'
           OR NEW.state IS DISTINCT FROM 'version_known'
           OR audit_exists THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_put_response_source_rejected';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.put_resolution_source = 'operator_reconciliation' THEN
        SELECT * INTO audit_row
          FROM marketplaces_marketplacefeedputreconciliationaudit
         WHERE attempt_id = NEW.id;
        IF NOT FOUND
           OR OLD.state IS DISTINCT FROM 'put_pending'
           OR NEW.state NOT IN ('no_object', 'version_known', 'manual_review')
           OR audit_row.pre_revision IS DISTINCT FROM OLD.revision
           OR audit_row.post_revision IS DISTINCT FROM NEW.revision
           OR audit_row.from_state IS DISTINCT FROM OLD.state
           OR audit_row.to_state IS DISTINCT FROM NEW.state
           OR audit_row.decision_code IS DISTINCT FROM NEW.safe_error_code
           OR audit_row.version_id_captured IS DISTINCT FROM (
               NEW.object_version_id IS NOT NULL
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_put_operator_source_rejected';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'feed_put_resolution_source_rejected';
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_put_resolution_source_guard_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
CREATE TRIGGER mkt_feed_put_resolution_source_guard_trg
BEFORE INSERT OR UPDATE
ON marketplaces_marketplacefeedartifactuploadattempt
FOR EACH ROW EXECUTE FUNCTION mkt_feed_put_resolution_source_guard_fn();


CREATE OR REPLACE FUNCTION mkt_feed_put_audit_pair_deferred_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    attempt_row marketplaces_marketplacefeedartifactuploadattempt%ROWTYPE;
    pair_is_valid boolean := false;
BEGIN
    SELECT * INTO attempt_row
      FROM marketplaces_marketplacefeedartifactuploadattempt
     WHERE id = NEW.attempt_id;

    IF attempt_row.id IS NOT NULL
       AND attempt_row.put_resolution_source = 'operator_reconciliation'
       AND NEW.version_id_captured IS NOT DISTINCT FROM (
           attempt_row.object_version_id IS NOT NULL
       ) THEN
        IF NEW.to_state = 'version_known' THEN
            pair_is_valid := (
                (
                    attempt_row.state = 'version_known'
                    AND attempt_row.revision = NEW.post_revision
                )
                OR (
                    attempt_row.state IN (
                        'verified', 'attached', 'orphaned', 'manual_review'
                    )
                    AND attempt_row.revision > NEW.post_revision
                )
            );
        ELSE
            pair_is_valid := (
                attempt_row.state = NEW.to_state
                AND attempt_row.revision = NEW.post_revision
                AND attempt_row.safe_error_code = NEW.decision_code
            );
        END IF;
    END IF;

    IF NOT pair_is_valid THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_put_audit_pair_commit_rejected';
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_put_audit_pair_deferred_trg
    ON marketplaces_marketplacefeedputreconciliationaudit;
CREATE CONSTRAINT TRIGGER mkt_feed_put_audit_pair_deferred_trg
AFTER INSERT ON marketplaces_marketplacefeedputreconciliationaudit
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION mkt_feed_put_audit_pair_deferred_fn();
'''

REVERSE_SQL = r'''

DO $$
BEGIN
    -- Keep the provenance guards installed after any use.  Use the live
    -- first-incompatible-write order described in the forward preflight.
    LOCK TABLE
        marketplaces_marketplacefeedartifact,
        marketplaces_marketplacefeedputreconciliationaudit,
        marketplaces_marketplacefeedartifactuploadattempt,
        marketplaces_marketplacefeedrun,
        marketplaces_marketplacefeedendpoint
    IN SHARE ROW EXCLUSIVE MODE;

    IF EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedputreconciliationaudit
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         WHERE put_resolution_source <> ''
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM marketplaces_marketplacefeedartifact LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE feed_artifact_id IS NOT NULL
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE artifact_upload_attempt <> 0
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedendpoint
         WHERE current_artifact_id IS NOT NULL
         LIMIT 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_put_audit_guard_reverse_preflight_failed';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_put_audit_pair_deferred_trg
    ON marketplaces_marketplacefeedputreconciliationaudit;
DROP FUNCTION IF EXISTS mkt_feed_put_audit_pair_deferred_fn();

DROP TRIGGER IF EXISTS mkt_feed_put_resolution_source_guard_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
DROP FUNCTION IF EXISTS mkt_feed_put_resolution_source_guard_fn();

DROP TRIGGER IF EXISTS mkt_feed_put_audit_guard_trg
    ON marketplaces_marketplacefeedputreconciliationaudit;
DROP FUNCTION IF EXISTS mkt_feed_put_audit_guard_fn();


DO $$
BEGIN
    -- A downgrade cannot preserve the pre-PUT journal contract once any
    -- generation used it. Keep every guard installed unless the dark schema
    -- is still completely empty, using the same deadlock-safe write order as
    -- the forward preflight.
    LOCK TABLE
        marketplaces_marketplacefeedartifact,
        marketplaces_marketplacefeedrun,
        marketplaces_marketplacefeedartifactuploadattempt,
        marketplaces_marketplacefeedendpoint
    IN SHARE ROW EXCLUSIVE MODE;

    IF EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedartifactuploadattempt
         LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM marketplaces_marketplacefeedartifact LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE feed_artifact_id IS NOT NULL
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedrun
         WHERE artifact_upload_attempt <> 0
         LIMIT 1
    ) OR EXISTS (
        SELECT 1
          FROM marketplaces_marketplacefeedendpoint
         WHERE current_artifact_id IS NOT NULL
         LIMIT 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_upload_ledger_reverse_preflight_failed';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_upload_attach_deferred_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
DROP TRIGGER IF EXISTS mkt_feed_run_attach_deferred_trg
    ON marketplaces_marketplacefeedrun;
DROP TRIGGER IF EXISTS mkt_feed_artifact_attach_deferred_trg
    ON marketplaces_marketplacefeedartifact;
DROP FUNCTION IF EXISTS mkt_feed_artifact_attach_deferred_fn();

DROP TRIGGER IF EXISTS mkt_feed_upload_guard_trg
    ON marketplaces_marketplacefeedartifactuploadattempt;
DROP FUNCTION IF EXISTS mkt_feed_upload_guard_fn();

CREATE OR REPLACE FUNCTION mkt_feed_artifact_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_row marketplaces_marketplaceaccount%ROWTYPE;
    endpoint_row marketplaces_marketplacefeedendpoint%ROWTYPE;
    run_row marketplaces_marketplacefeedrun%ROWTYPE;
    tenant_row tenants_tenant%ROWTYPE;
    expected_object_key text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_artifact_update_forbidden';
    END IF;

    SELECT * INTO owner_row
      FROM marketplaces_marketplaceaccount
     WHERE id = NEW.account_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;
    SELECT * INTO endpoint_row
      FROM marketplaces_marketplacefeedendpoint
     WHERE public_id = NEW.endpoint_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;
    SELECT * INTO run_row
      FROM marketplaces_marketplacefeedrun
     WHERE id = NEW.run_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;
    SELECT * INTO tenant_row
      FROM tenants_tenant
     WHERE id = owner_row.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    IF owner_row.deleted_at IS NOT NULL
       OR owner_row.is_active IS NOT TRUE
       OR tenant_row.is_active IS NOT TRUE
       OR endpoint_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.account_id IS DISTINCT FROM owner_row.id
       OR run_row.tenant_id IS DISTINCT FROM owner_row.tenant_id
       OR run_row.marketplace IS DISTINCT FROM owner_row.marketplace
       OR run_row.account_identity_digest IS DISTINCT FROM endpoint_row.owner_identity_digest
       OR owner_row.feed_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.source_intent_revision IS DISTINCT FROM endpoint_row.source_intent_revision
       OR run_row.endpoint_revision IS DISTINCT FROM endpoint_row.artifact_revision
       OR run_row.predecessor_artifact_id IS DISTINCT FROM endpoint_row.current_artifact_id
       OR run_row.artifact_upload_attempt IS DISTINCT FROM NEW.upload_attempt
       OR run_row.payload_sha256 IS NULL
       OR run_row.payload_sha256 = ''
       OR run_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;

    expected_object_key := format(
        'private-feeds/v1/%s/%s/%s/feed.xml',
        NEW.endpoint_id::text,
        NEW.run_id::text,
        lpad(NEW.upload_attempt::text, 5, '0')
    );
    IF NEW.object_key IS DISTINCT FROM expected_object_key THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'feed_artifact_insert_rejected';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_artifact_guard_trg
    ON marketplaces_marketplacefeedartifact;
CREATE TRIGGER mkt_feed_artifact_guard_trg
BEFORE INSERT OR UPDATE ON marketplaces_marketplacefeedartifact
FOR EACH ROW EXECUTE FUNCTION mkt_feed_artifact_guard_fn();

CREATE OR REPLACE FUNCTION mkt_feed_run_art_guard_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    artifact_row marketplaces_marketplacefeedartifact%ROWTYPE;
    artifact_exists boolean;
BEGIN
    IF OLD.feed_artifact_id IS DISTINCT FROM NEW.feed_artifact_id THEN
        IF OLD.feed_artifact_id IS NOT NULL OR NEW.feed_artifact_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_mutation_forbidden';
        END IF;
        SELECT * INTO artifact_row
          FROM marketplaces_marketplacefeedartifact
         WHERE id = NEW.feed_artifact_id;
        IF NOT FOUND
           OR artifact_row.run_id IS DISTINCT FROM OLD.id
           OR artifact_row.account_id IS DISTINCT FROM NEW.account_id
           OR artifact_row.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
           OR artifact_row.upload_attempt IS DISTINCT FROM NEW.artifact_upload_attempt THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'feed_run_artifact_selection_rejected';
        END IF;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM marketplaces_marketplacefeedartifact WHERE run_id = OLD.id
    ) INTO artifact_exists;
    IF (NEW.feed_artifact_id IS NOT NULL OR artifact_exists)
       AND (
           NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.marketplace IS DISTINCT FROM OLD.marketplace
           OR NEW.account_identity_digest IS DISTINCT FROM OLD.account_identity_digest
           OR NEW.source_intent_revision IS DISTINCT FROM OLD.source_intent_revision
           OR NEW.endpoint_revision IS DISTINCT FROM OLD.endpoint_revision
           OR NEW.predecessor_artifact_id IS DISTINCT FROM OLD.predecessor_artifact_id
           OR NEW.artifact_upload_attempt IS DISTINCT FROM OLD.artifact_upload_attempt
           OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'feed_run_artifact_snapshot_mutation_forbidden';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mkt_feed_run_art_guard_trg
    ON marketplaces_marketplacefeedrun;
CREATE TRIGGER mkt_feed_run_art_guard_trg
BEFORE UPDATE ON marketplaces_marketplacefeedrun
FOR EACH ROW EXECUTE FUNCTION mkt_feed_run_art_guard_fn();


DROP TRIGGER IF EXISTS mkt_feed_fetch_guard_trg
    ON marketplaces_marketplacefeedfetchevidence;
DROP TRIGGER IF EXISTS mkt_feed_run_art_guard_trg
    ON marketplaces_marketplacefeedrun;
DROP TRIGGER IF EXISTS mkt_feed_endpoint_art_guard_trg
    ON marketplaces_marketplacefeedendpoint;
DROP TRIGGER IF EXISTS mkt_feed_artifact_guard_trg
    ON marketplaces_marketplacefeedartifact;

DROP FUNCTION IF EXISTS mkt_feed_fetch_guard_fn();
DROP FUNCTION IF EXISTS mkt_feed_run_art_guard_fn();
DROP FUNCTION IF EXISTS mkt_feed_endpoint_art_guard_fn();
DROP FUNCTION IF EXISTS mkt_feed_artifact_guard_fn();
'''


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0029_private_feed_artifacts'),
    ]

    operations = [
        migrations.RunSQL(
            sql=FORWARD_SQL,
            reverse_sql=REVERSE_SQL,
        ),
    ]
