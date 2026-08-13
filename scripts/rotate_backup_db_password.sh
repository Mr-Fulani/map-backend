#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Rotate the dedicated read-only backup login without ever printing either the
# old or new password.  The replacement env file is prepared and fsynced before
# PostgreSQL changes, then atomically installed immediately after ALTER ROLE.
ROOT_DIR="/opt/saas_poster"
BACKUP_ENV_FILE="$ROOT_DIR/.backup.env"
ROTATION_UNCERTAIN_FILE="$ROOT_DIR/.backup-db-rotation-uncertain"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"

fail() {
  echo "backup DB credential rotation failed: $*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || fail "must run as root"
/usr/local/sbin/saas-poster-validate-checkout >/dev/null \
  || fail "canonical checkout ownership or permissions are unsafe"
[[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
  || fail "deploy lock directory is unavailable"
[[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
  || fail "deploy lock directory must have mode 700"
exec 9>"$DEPLOY_LOCK_FILE"
flock 9

if [[ -e "$ROTATION_UNCERTAIN_FILE" || -L "$ROTATION_UNCERTAIN_FILE" ]]; then
  [[ -f "$ROTATION_UNCERTAIN_FILE" && ! -L "$ROTATION_UNCERTAIN_FILE" \
      && -O "$ROTATION_UNCERTAIN_FILE" ]] \
    || fail "an unsafe unresolved rotation marker requires manual recovery"
  case "$(stat -c '%a' "$ROTATION_UNCERTAIN_FILE")" in
    400|600) ;;
    *) fail "unresolved rotation marker has unsafe permissions" ;;
  esac
  IFS= read -r recovery_env <"$ROTATION_UNCERTAIN_FILE" || true
  [[ "$recovery_env" == "$ROOT_DIR"/.backup.env.next.* \
      && -f "$recovery_env" && ! -L "$recovery_env" && -O "$recovery_env" ]] \
    || fail "unresolved rotation marker or recovery env is invalid"
  case "$(stat -c '%a' "$recovery_env")" in
    400|600) ;;
    *) fail "unresolved rotation recovery env has unsafe permissions" ;;
  esac
  fail "a previous password change may have committed; reconcile the root-only recovery env at $recovery_env before retrying"
fi

[[ -f "$BACKUP_ENV_FILE" && ! -L "$BACKUP_ENV_FILE" && -O "$BACKUP_ENV_FILE" ]] \
  || fail ".backup.env must be a root-owned regular file"
case "$(stat -c '%a' "$BACKUP_ENV_FILE")" in 400|600) ;; *) fail "unsafe .backup.env mode" ;; esac

credential_tmp="$(mktemp "$ROOT_DIR/.backup-db-credential.XXXXXX")"
env_tmp="$(mktemp "$ROOT_DIR/.backup.env.next.XXXXXX")"
publish_tmp="$(mktemp "$ROOT_DIR/.backup.env.publish.XXXXXX")"
marker_tmp="$(mktemp "$ROOT_DIR/.backup-db-rotation-marker.XXXXXX")"
password_change_may_have_happened=false
rotation_resolved=false
cleanup() {
  rm -f -- "$credential_tmp"
  rm -f -- "$publish_tmp"
  rm -f -- "$marker_tmp"
  unset backup_password 2>/dev/null || true
  if [[ "$rotation_resolved" != "true" \
      && ( "$password_change_may_have_happened" == "true" \
        || -e "$ROTATION_UNCERTAIN_FILE" ) ]]; then
    # Once the durable marker exists, a killed/failed psql client cannot prove
    # whether ALTER ROLE committed. Never destroy the only prepared credential.
    echo "CRITICAL: the database password may have changed; rotation is unresolved." >&2
    echo "Recovery env preserved at: $env_tmp" >&2
  else
    rm -f -- "$env_tmp"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 0600 "$credential_tmp" "$env_tmp" "$publish_tmp" "$marker_tmp"

python3 - "$BACKUP_ENV_FILE" "$env_tmp" "$publish_tmp" "$credential_tmp" <<'PY'
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

source_path, recovery_path, publish_path, credential_path = map(Path, sys.argv[1:])
lines = source_path.read_text().splitlines(keepends=True)
matches = [i for i, line in enumerate(lines) if line.startswith('BACKUP_DATABASE_URL=')]
if len(matches) != 1:
    raise SystemExit('expected exactly one BACKUP_DATABASE_URL')

index = matches[0]
raw_value = lines[index].split('=', 1)[1].rstrip('\r\n')
quote_char = raw_value[:1] if raw_value[:1] in {'"', "'"} else ''
if quote_char:
    if not raw_value.endswith(quote_char):
        raise SystemExit('unbalanced BACKUP_DATABASE_URL quotes')
    raw_value = raw_value[1:-1]

parsed = urlsplit(raw_value)
role = unquote(parsed.username or '')
if (
    parsed.scheme not in {'postgres', 'postgresql'}
    or not role
    or not (role[0].isalpha() or role[0] == '_')
    or any(not (char.isalnum() or char == '_') for char in role)
    or parsed.hostname != 'db'
    or not parsed.path.lstrip('/')
):
    raise SystemExit('BACKUP_DATABASE_URL does not match the production DB contract')

password = secrets.token_urlsafe(48)
host = parsed.hostname
if parsed.port is not None:
    host = f'{host}:{parsed.port}'
netloc = f'{quote(role, safe="")}:{quote(password, safe="")}@{host}'
new_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
newline = '\n' if lines[index].endswith('\n') else ''
value = f'{quote_char}{new_url}{quote_char}' if quote_char else new_url
lines[index] = f'BACKUP_DATABASE_URL={value}{newline}'

for target_path in (recovery_path, publish_path):
    with target_path.open('w') as target:
        target.writelines(lines)
        target.flush()
        os.fsync(target.fileno())
with credential_path.open('w') as credentials:
    credentials.write(f'{role}\n{password}\n')
    credentials.flush()
    os.fsync(credentials.fileno())
PY

fsync_root_dir() {
  python3 - "$ROOT_DIR" <<'PY'
import os
import sys

flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
directory_fd = os.open(sys.argv[1], flags)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

fsync_file() {
  python3 - "$1" <<'PY'
import os
import sys

file_fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(file_fd)
finally:
    os.close(file_fd)
PY
}

{
  IFS= read -r backup_role
  IFS= read -r backup_password
} <"$credential_tmp"
[[ "$backup_role" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && -n "$backup_password" ]] \
  || fail "generated credential metadata is invalid"

# Finish every operation that can be validated before changing PostgreSQL.
# From this point on env_tmp is already a durable root-only recovery copy.
chown root:root "$env_tmp"
chown root:root "$publish_tmp"
chmod 0600 "$env_tmp" "$publish_tmp"
# Persist both root-only directory entries before PostgreSQL starts accepting
# the new password. The recovery copy is not the file that will be renamed.
fsync_root_dir

COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$ROOT_DIR/docker-compose.prod.yml"
)

# Refuse to rotate an application/admin/elevated login.  This checks the
# effective privileges in the current database, not just the role attributes;
# the only allowed membership is the built-in read-only pg_read_all_data role.
# shellcheck disable=SC2016  # Expand PostgreSQL env vars inside the container.
role_contract="$({
  printf '\\set map_backup_role %s\n' "$backup_role"
  cat <<'SQL'
WITH RECURSIVE
target AS (
  SELECT oid, rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
         rolcanlogin, rolreplication, rolbypassrls
    FROM pg_catalog.pg_roles
   WHERE rolname = :'map_backup_role'
),
memberships(roleid) AS (
  SELECT membership.roleid
    FROM pg_catalog.pg_auth_members AS membership
    JOIN target ON target.oid = membership.member
  UNION
  SELECT membership.roleid
    FROM pg_catalog.pg_auth_members AS membership
    JOIN memberships AS inherited ON inherited.roleid = membership.member
),
unsafe_table_privilege AS (
  SELECT 1
    FROM target
    JOIN pg_catalog.pg_class AS relation ON true
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
   WHERE namespace.nspname !~ '^pg_'
     AND namespace.nspname <> 'information_schema'
     AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
     AND (
       pg_catalog.has_table_privilege(target.rolname, relation.oid, 'INSERT')
       OR pg_catalog.has_table_privilege(target.rolname, relation.oid, 'UPDATE')
       OR pg_catalog.has_table_privilege(target.rolname, relation.oid, 'DELETE')
       OR pg_catalog.has_table_privilege(target.rolname, relation.oid, 'TRUNCATE')
       OR pg_catalog.has_table_privilege(target.rolname, relation.oid, 'REFERENCES')
       OR pg_catalog.has_table_privilege(target.rolname, relation.oid, 'TRIGGER')
     )
),
unsafe_sequence_privilege AS (
  SELECT 1
    FROM target
    JOIN pg_catalog.pg_class AS sequence ON sequence.relkind = 'S'
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = sequence.relnamespace
   WHERE namespace.nspname !~ '^pg_'
     AND namespace.nspname <> 'information_schema'
     AND (
       pg_catalog.has_sequence_privilege(target.rolname, sequence.oid, 'USAGE')
       OR pg_catalog.has_sequence_privilege(target.rolname, sequence.oid, 'UPDATE')
     )
),
unsafe_ownership AS (
  SELECT 1 FROM target
   WHERE EXISTS (
     SELECT 1 FROM pg_catalog.pg_shdepend AS dependency
      WHERE dependency.refclassid = 'pg_catalog.pg_authid'::regclass
        AND dependency.refobjid = target.oid
        AND dependency.deptype = 'o'
   )
      OR EXISTS (
     SELECT 1 FROM pg_catalog.pg_database AS database
      WHERE database.datdba = target.oid
   )
      OR EXISTS (
     SELECT 1 FROM pg_catalog.pg_namespace AS namespace
      WHERE namespace.nspname !~ '^pg_'
        AND namespace.nspname <> 'information_schema'
        AND namespace.nspowner = target.oid
   )
      OR EXISTS (
     SELECT 1
       FROM pg_catalog.pg_class AS relation
       JOIN pg_catalog.pg_namespace AS namespace
         ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname !~ '^pg_'
        AND namespace.nspname <> 'information_schema'
        AND relation.relowner = target.oid
   )
      OR EXISTS (
     SELECT 1
       FROM pg_catalog.pg_proc AS procedure
       JOIN pg_catalog.pg_namespace AS namespace
         ON namespace.oid = procedure.pronamespace
      WHERE namespace.nspname !~ '^pg_'
        AND namespace.nspname <> 'information_schema'
        AND procedure.proowner = target.oid
   )
      OR EXISTS (
     SELECT 1 FROM pg_catalog.pg_largeobject_metadata AS large_object
      WHERE large_object.lomowner = target.oid
   )
),
unsafe_security_definer AS (
  SELECT 1
    FROM target
    JOIN pg_catalog.pg_proc AS procedure ON procedure.prosecdef
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
   WHERE namespace.nspname !~ '^pg_'
     AND namespace.nspname <> 'information_schema'
     AND pg_catalog.has_function_privilege(
       target.rolname,
       procedure.oid,
       'EXECUTE'
     )
),
unsafe_default_acl AS (
  SELECT 1
    FROM target
    JOIN pg_catalog.pg_default_acl AS defaults ON true
    CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
   WHERE (
       acl.grantee = 0
       OR acl.grantee = target.oid
       OR acl.grantee IN (SELECT roleid FROM memberships)
     )
     AND (
       (defaults.defaclobjtype = 'r' AND acl.privilege_type IN (
         'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'
       ))
       OR (defaults.defaclobjtype = 'S' AND acl.privilege_type IN (
         'USAGE', 'UPDATE'
       ))
       OR (defaults.defaclobjtype = 'n' AND acl.privilege_type = 'CREATE')
     )
)
SELECT CASE WHEN
  current_setting('password_encryption') = 'scram-sha-256'
  AND
  COALESCE((
    SELECT rolcanlogin
       AND rolinherit
       AND NOT rolsuper
       AND NOT rolcreaterole
       AND NOT rolcreatedb
       AND NOT rolreplication
       AND NOT rolbypassrls
       AND rolname <> current_user
      FROM target
  ), false)
  AND EXISTS (
    SELECT 1
      FROM memberships
      JOIN pg_catalog.pg_roles AS granted_role
        ON granted_role.oid = memberships.roleid
     WHERE granted_role.rolname = 'pg_read_all_data'
  )
  AND NOT EXISTS (
    SELECT 1
      FROM memberships
      JOIN pg_catalog.pg_roles AS granted_role
        ON granted_role.oid = memberships.roleid
     WHERE granted_role.rolname <> 'pg_read_all_data'
  )
  AND COALESCE((
    SELECT pg_catalog.has_database_privilege(
             rolname,
             current_database(),
             'CONNECT'
           )
       AND NOT pg_catalog.has_database_privilege(
             rolname,
             current_database(),
             'CREATE'
           )
      FROM target
  ), false)
  AND NOT EXISTS (
    SELECT 1
      FROM target
      JOIN pg_catalog.pg_namespace AS namespace ON true
     WHERE namespace.nspname !~ '^pg_'
       AND namespace.nspname <> 'information_schema'
       AND pg_catalog.has_schema_privilege(
         target.rolname,
         namespace.oid,
         'CREATE'
       )
  )
  AND NOT EXISTS (SELECT 1 FROM unsafe_table_privilege)
  AND NOT EXISTS (SELECT 1 FROM unsafe_sequence_privilege)
  AND NOT EXISTS (SELECT 1 FROM unsafe_ownership)
  AND NOT EXISTS (SELECT 1 FROM unsafe_security_definer)
  AND NOT EXISTS (SELECT 1 FROM unsafe_default_acl)
THEN 'safe' ELSE 'unsafe' END;
SQL
} | "${COMPOSE[@]}" exec -T db sh -eu -c \
  'exec psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"')"
[[ "$role_contract" == "safe" ]] \
  || fail "backup DB role is missing, privileged, writable, or not distinct from POSTGRES_USER"

# Persist a root-only may-have-changed marker before crossing the PostgreSQL
# boundary.  SIGKILL, reboot, a lost Docker client, and an ambiguous psql exit
# therefore all leave a durable pointer to the already-fsynced recovery env.
printf '%s\n' "$env_tmp" >"$marker_tmp"
chown root:root "$marker_tmp"
chmod 0600 "$marker_tmp"
fsync_file "$marker_tmp"
mv -f -- "$marker_tmp" "$ROTATION_UNCERTAIN_FILE"
marker_tmp=""
fsync_root_dir
password_change_may_have_happened=true

# \password reads the new value twice from psql stdin and sends only the
# PostgreSQL password verifier in ALTER ROLE.  The cleartext is never present
# in docker/psql argv, docker exec environment, or server SQL logs.
# shellcheck disable=SC2016  # Expand PostgreSQL env vars inside the container.
{
  printf '\\password %s\n' "$backup_role"
  printf '%s\n%s\n' "$backup_password" "$backup_password"
} | "${COMPOSE[@]}" exec -T db sh -eu -c \
  'exec psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
unset backup_password

# From here through durable marker removal, suppress every catchable signal that
# could run EXIT cleanup between publishing .backup.env and clearing the marker.
# The section is only atomic rename + fsync; SIGKILL still leaves marker/env for
# recovery, while ordinary signal handling is restored immediately afterwards.
trap '' HUP INT TERM

mv -f -- "$publish_tmp" "$BACKUP_ENV_FILE"
# Persist the atomic rename before declaring the credential installed. If this
# fails, cleanup retains the separate recovery copy at env_tmp.
fsync_root_dir
rm -f -- "$credential_tmp"

# The installed env rename is durable. Clearing and fsyncing the marker is the
# commit record for the host-side half of the rotation.
rm -f -- "$ROTATION_UNCERTAIN_FILE"
fsync_root_dir
rotation_resolved=true
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# A successful new dump proves that the role and atomic env replacement agree.
# Release the credential cutover lock first: the installed backup entrypoint takes the
# same lock itself, so deploy/other rotations remain serialized without a
# self-deadlock during the verification backup.
flock -u 9
exec 9>&-
/usr/local/sbin/saas-poster-backup
echo "backup DB credential rotated and verified by a new encrypted backup"
