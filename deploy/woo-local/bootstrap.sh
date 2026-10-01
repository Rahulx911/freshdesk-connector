#!/usr/bin/env bash
# Install WordPress + WooCommerce in the running containers, seed fictional
# Kettle & Leaf data, and mint a READ-ONLY REST API key.
#
#   docker compose up -d && ./bootstrap.sh
#
# Everything here is local and disposable. The admin password is fixed on
# purpose: this stack is a test fixture, not a deployment.
set -euo pipefail

cd "$(dirname "$0")"

SITE_URL="${SITE_URL:-http://localhost:8080}"
ADMIN_USER="${ADMIN_USER:-shopmanager}"
ADMIN_PASS="${ADMIN_PASS:-localdev-only-pass}"
ADMIN_EMAIL="${ADMIN_EMAIL:-shopmanager@kettleandleaf.test}"

# wp-cli runs as a compose service sharing the WordPress volume.
wp() {
  docker compose run --rm -T wpcli "$@"
}

echo "==> waiting for WordPress"
for _ in $(seq 1 60); do
  if curl -fsS "$SITE_URL" >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "==> installing WordPress"
if ! wp core is-installed 2>/dev/null; then
  wp core install \
    --url="$SITE_URL" \
    --title="Kettle & Leaf" \
    --admin_user="$ADMIN_USER" \
    --admin_password="$ADMIN_PASS" \
    --admin_email="$ADMIN_EMAIL" \
    --skip-email
else
  echo "    already installed"
fi

echo "==> installing WooCommerce"
wp plugin is-active woocommerce 2>/dev/null || wp plugin install woocommerce --activate
wp option update woocommerce_currency INR
wp option update permalink_structure '/%postname%/'
wp rewrite flush --hard

echo "==> seeding fictional store data"
docker compose cp seed.php wordpress:/var/www/html/seed.php
wp eval-file seed.php

echo "==> creating a READ-ONLY REST API key"
# WooCommerce stores the consumer key as an HMAC-SHA256 hash and the secret in
# clear, so the key can only be shown once, here.
KEYS=$(wp eval '
$user = get_user_by("login", "'"$ADMIN_USER"'");
$ck = "ck_" . bin2hex(random_bytes(20));
$cs = "cs_" . bin2hex(random_bytes(20));
global $wpdb;
$wpdb->insert($wpdb->prefix . "woocommerce_api_keys", [
    "user_id"         => $user->ID,
    "description"     => "Agent Studio connector (read only)",
    "permissions"     => "read",
    "consumer_key"    => wc_api_hash($ck),
    "consumer_secret" => $cs,
    "truncated_key"   => substr($ck, -7),
], ["%d","%s","%s","%s","%s","%s"]);
echo $ck . " " . $cs;
')

CK=$(echo "$KEYS" | tr -d '\r' | awk '{print $1}')
CS=$(echo "$KEYS" | tr -d '\r' | awk '{print $2}')

cat <<SUMMARY

================================================================
  Local WooCommerce is ready.

  Store      $SITE_URL
  Admin      $SITE_URL/wp-admin  ($ADMIN_USER / $ADMIN_PASS)
  Key scope  read   (this key physically cannot write)

  Run the connector against it:

    export WOO_STORE_URL=$SITE_URL
    export WOO_CONSUMER_KEY=$CK
    export WOO_CONSUMER_SECRET=$CS
    python scripts/demo.py --live

  Tear down with:  docker compose down -v
================================================================
SUMMARY
