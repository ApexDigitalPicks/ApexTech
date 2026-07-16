#!/bin/bash
# Scout Phase 1 deploy — schema + WhatsApp hook + verify view.
# Safe to re-run. Gmail ingestion (scout_gmail_ingest.py) is deployed
# separately once OAuth is set up locally — see its module docstring.
set -e
cd /home/apexflow/apexflow-deploy
SRC="$(cd "$(dirname "$0")" && pwd)"
CLIENT_DIR="clients/apexdigitalpicks"

echo "=== 1. Checking files ==="
for f in scout_common.py scout_contacts.py scout_persona.yaml scout_identity.py \
         scout_migrate.py scout_whatsapp_hook.py scout_verify.py; do
  test -s "$SRC/$f" || { echo "MISSING OR EMPTY: $f. Re-upload it to GitHub."; exit 1; }
done

echo "=== 2. Dry-run schema migration against the live DB (no writes) ==="
DB_PATH=$(find "$CLIENT_DIR/data" -maxdepth 1 -name '*.db' | head -n1)
test -n "$DB_PATH" || { echo "No .db file found under $CLIENT_DIR/data. Aborting."; exit 1; }
python3 "$SRC/scout_migrate.py" "$DB_PATH"
echo ""
read -p "Dry run above OK? Apply migration to $DB_PATH? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || { echo "Aborted, no changes made."; exit 1; }
python3 "$SRC/scout_migrate.py" "$DB_PATH" --apply

echo "=== 3. Copying Scout files into $CLIENT_DIR ==="
cp "$SRC"/scout_common.py "$SRC"/scout_contacts.py "$SRC"/scout_persona.yaml \
   "$SRC"/scout_identity.py "$SRC"/scout_whatsapp_hook.py "$SRC"/scout_verify.py \
   "$CLIENT_DIR/"

echo "=== 4. Patching app.py (only if not already patched) ==="
if grep -q "scout_verify_bp" "$CLIENT_DIR/app.py"; then
  echo "app.py already registers Scout's verify view, skipping."
else
  cat >> "$CLIENT_DIR/app.py" << 'EOF'

try:
    from scout_verify import scout_verify_bp
    app.register_blueprint(scout_verify_bp)
except Exception as e:
    print(f"Scout verify view not loaded: {e}")
EOF
  echo "Patched."
fi
echo "NOTE: the WhatsApp write-hook (scout_whatsapp_hook.log_whatsapp_activity)"
echo "still needs a manual call added inside the existing webhook handler function --"
echo "see scout_whatsapp_hook.py's INTEGRATION docstring. This script does not guess"
echo "where that call belongs inside your webhook handler."

echo "=== 5. Installing Python deps ==="
pip install -r "$SRC/requirements-scout.txt"

echo "=== 6. Building image ==="
docker compose build apexflow-apexdigitalpicks

echo "=== 7. Import test in a throwaway container (live bot untouched) ==="
docker run --rm --env-file "$CLIENT_DIR/.env" \
  --entrypoint python apexflow-deploy-apexflow-apexdigitalpicks \
  -c "import app; print('IMPORT OK')" || { echo "IMPORT FAILED. Live bot NOT restarted."; exit 1; }

echo "=== 8. Restarting with new image ==="
docker compose up -d apexflow-apexdigitalpicks
sleep 5
docker ps --format 'table {{.Names}}\t{{.Status}}'

echo "=== 9. Verify view health check ==="
CODE=$(docker exec apexflow-apexdigitalpicks curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:5000/scout/api/verify/summary)
echo "HTTP $CODE (401 is fine here -- means the route is up and just needs the access key)"
