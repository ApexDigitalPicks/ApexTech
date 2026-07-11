#!/bin/bash
# ApexFlow dashboard deploy, one shot, safe to re-run.
set -e
cd /home/apexflow/apexflow-deploy
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. Checking files ==="
for f in dashboard_api.py dashboard.html; do
  test -s "$SRC/$f" || { echo "MISSING OR EMPTY: $f. Re-upload it to GitHub."; exit 1; }
done
ls -la "$SRC"/dashboard_api.py "$SRC"/dashboard.html

echo "=== 2. Copying into clients/bambino ==="
cp "$SRC/dashboard_api.py" "$SRC/dashboard.html" clients/bambino/

echo "=== 3. Patching app.py (only if not already patched) ==="
if grep -q "dashboard_bp" clients/bambino/app.py; then
  echo "app.py already registers the dashboard, skipping."
else
  cat >> clients/bambino/app.py << 'EOF'

try:
    from dashboard_api import dashboard_bp
    app.register_blueprint(dashboard_bp)
except Exception as e:
    print(f"Dashboard not loaded: {e}")
EOF
  echo "Patched."
fi

echo "=== 4. Ensuring access key exists in .env ==="
if grep -q "DASHBOARD_ACCESS_KEY" clients/bambino/.env; then
  echo "Key already set, keeping it."
else
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  echo "DASHBOARD_ACCESS_KEY=$KEY" >> clients/bambino/.env
fi

echo "=== 5. Building image ==="
docker compose build apexflow-bambino

echo "=== 6. Import test in a throwaway container (live bot untouched) ==="
docker run --rm --env-file clients/bambino/.env \
  --entrypoint python apexflow-deploy-apexflow-bambino \
  -c "import app; print('IMPORT OK')" || { echo "IMPORT FAILED. Live bot NOT restarted."; exit 1; }

echo "=== 7. Restarting with new image ==="
docker compose up -d apexflow-bambino
sleep 5
docker ps --format 'table {{.Names}}\t{{.Status}}'

echo "=== 8. Dashboard health check ==="
CODE=$(docker exec apexflow-bambino curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/dashboard/)
echo "HTTP $CODE"
test "$CODE" = "200" && echo "=== SUCCESS. Open /dashboard/ on your phone. ===" || echo "Dashboard returned $CODE, send this output to Claude."

echo ""
echo "Your access key is:"
grep DASHBOARD_ACCESS_KEY clients/bambino/.env
