#!/bin/bash
# One-time root bootstrap of the generic deployment helper (re-run to update it; idempotent):
#   sudo bash deploy/install.sh
# - installs deploy/edge_deploy.py as /usr/local/sbin/edge-app-deploy (the previous copy is kept as a backup)
# - creates the restricted account edge-app-deploy (no password, no shell for anything but the helper)
# - gives every runner user (edge-ci = renecaldeira, edge-ci-fernando = fespadoni) its own SSH key, accepted
#   only from 127.0.0.1 and only to run the helper for that owner
# - creates /etc/edge-app-deploy/apps (root-owned app configurations) and /var/lib/edge-app-deploy (state)
# Runner slots run with NoNewPrivileges, so a job reaches the helper over SSH to localhost, never through sudo.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root' >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
declare -A RUNNERS=([edge-ci]=renecaldeira [edge-ci-fernando]=fespadoni)

python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$here/edge_deploy.py"
# leftovers of a first attempt under the name edge-deploy, which belongs to edge-admin's deploy account
if [[ -f /usr/local/sbin/edge-deploy ]] && grep -q 'Generic deployment helper for apps on the Edge VPS' /usr/local/sbin/edge-deploy; then
  rm -f /usr/local/sbin/edge-deploy
fi
rmdir /etc/edge-deploy/apps /etc/edge-deploy 2>/dev/null || true
if [[ -f /usr/local/sbin/edge-app-deploy ]]; then
  cp -p /usr/local/sbin/edge-app-deploy "/usr/local/sbin/edge-app-deploy.bak-$(date +%Y%m%d-%H%M%S)"
fi
install -o root -g root -m 0755 "$here/edge_deploy.py" /usr/local/sbin/edge-app-deploy
install -d -o root -g root -m 0755 /etc/edge-app-deploy /etc/edge-app-deploy/apps
install -d -o root -g root -m 0700 /var/lib/edge-app-deploy

if ! id edge-app-deploy >/dev/null 2>&1; then
  useradd --create-home --home-dir /home/edge-app-deploy --user-group --shell /bin/sh edge-app-deploy
  passwd -l edge-app-deploy >/dev/null
fi
chown root:root /home/edge-app-deploy; chmod 0755 /home/edge-app-deploy
install -d -o root -g root -m 0755 /home/edge-app-deploy/.ssh

sudoers="$(mktemp)"
{
  echo '# edge-app-deploy may run the helper only with an owner fixed by its SSH forced command'
  echo 'Defaults:edge-app-deploy env_keep += "SSH_ORIGINAL_COMMAND"'
  for owner in "${RUNNERS[@]}"; do echo "edge-app-deploy ALL=(root) NOPASSWD: /usr/local/sbin/edge-app-deploy ${owner}"; done
} > "$sudoers"
visudo -cf "$sudoers" >/dev/null
install -o root -g root -m 0440 "$sudoers" /etc/sudoers.d/edge-app-deploy
rm -f "$sudoers"

keys="$(mktemp)"
for runner in "${!RUNNERS[@]}"; do
  owner="${RUNNERS[$runner]}"
  home="$(getent passwd "$runner" | cut -d: -f6)"
  [[ -n "$home" ]] || { echo "Missing runner user $runner" >&2; exit 1; }
  install -d -o "$runner" -g "$runner" -m 0700 "$home/.ssh"
  if [[ ! -f "$home/.ssh/edge-app-deploy" ]]; then
    runuser -u "$runner" -- ssh-keygen -q -t ed25519 -N "" -C "$runner edge-app-deploy" -f "$home/.ssh/edge-app-deploy"
  fi
  printf 'restrict,from="127.0.0.1,::1",command="sudo -n /usr/local/sbin/edge-app-deploy %s" ssh-ed25519 %s %s\n' \
    "$owner" "$(cut -d' ' -f2 "$home/.ssh/edge-app-deploy.pub")" "$runner" >> "$keys"
  ssh-keyscan -t ed25519 127.0.0.1 2>/dev/null > "$home/.ssh/known_hosts_edge_app_deploy"
  chown "$runner:$runner" "$home/.ssh/known_hosts_edge_app_deploy"; chmod 0600 "$home/.ssh/known_hosts_edge_app_deploy"
done
install -o root -g root -m 0644 "$keys" /home/edge-app-deploy/.ssh/authorized_keys
rm -f "$keys"
visudo -c >/dev/null

echo "--- installed"
ls -l /usr/local/sbin/edge-app-deploy /etc/sudoers.d/edge-app-deploy /home/edge-app-deploy/.ssh/authorized_keys
cut -c1-80 /home/edge-app-deploy/.ssh/authorized_keys
ls /etc/edge-app-deploy/apps
# the helper refuses an unknown app: proves each key, the forced command and sudo work
for runner in "${!RUNNERS[@]}"; do
  home="$(getent passwd "$runner" | cut -d: -f6)"
  printf '%s: ' "$runner"
  runuser -u "$runner" -- ssh -T -i "$home/.ssh/edge-app-deploy" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o UserKnownHostsFile="$home/.ssh/known_hosts_edge_app_deploy" -o StrictHostKeyChecking=yes \
    edge-app-deploy@127.0.0.1 "deploy nothing-here ${RANDOM}" < /dev/null 2>&1 || true
done
