#!/bin/bash
# Start command for a Beekeeper Runpod pod. Passed base64-encoded in BK_BOOTSTRAP.
#
# Starts sshd with the per-run public key, then waits for Beekeeper to drive the
# run over SSH. The pod protects itself: when BK_DEADLINE_S runs out (measured
# from the first container start) and Beekeeper hasn't released it, it terminates
# itself with the pod-scoped RUNPOD_API_KEY Runpod injects. Exiting the container
# would not help: Runpod restarts it and keeps billing.
mkdir -p /bk/run /bk/workspace /bk/inputs /bk/data /root/.ssh
exec > >(tee -a /bk/bootstrap.log) 2>&1
[ -f /bk/.started ] || date +%s > /bk/.started
started=$(cat /bk/.started)
echo "[bootstrap] $(date -u +%FT%TZ) start (first start $started, deadline ${BK_DEADLINE_S}s)"

printf '%s\n' "$BK_PUBLIC_KEY" > /root/.ssh/authorized_keys
chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys

need=""
[ -x /usr/sbin/sshd ] || need="$need openssh-server"
command -v rsync >/dev/null || need="$need rsync"
command -v curl >/dev/null || need="$need curl"
if [ -n "$need" ]; then
    echo "[bootstrap] installing:$need"
    (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need) >/dev/null 2>&1 \
        || echo "[bootstrap] WARNING: could not install$need"
fi
mkdir -p /run/sshd && ssh-keygen -A >/dev/null 2>&1
/usr/sbin/sshd -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no
touch /bk/ready
echo "[bootstrap] sshd up"

while true; do
    if [ -f /bk/release ]; then
        echo "[bootstrap] released by Beekeeper; idling until terminated"
        exec sleep infinity
    fi
    if [ $(( $(date +%s) - started )) -ge "${BK_DEADLINE_S}" ]; then
        break
    fi
    sleep 5
done

echo "[bootstrap] $(date -u +%FT%TZ) deadline reached without release, self-terminating"
for i in 1 2 3 4 5 6; do
    code=$(curl -s -o /tmp/bk-term.out -w '%{http_code}' -X POST \
        "https://api.runpod.io/v2/pods/${RUNPOD_POD_ID}/action" \
        -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H 'Content-Type: application/json' \
        -d '{"action":"terminate"}')
    echo "[bootstrap] terminate attempt $i: HTTP $code"
    case "$code" in 2*) break ;; esac
    sleep 30
done
exec sleep infinity
