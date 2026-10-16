#!/bin/bash
# One-time root bootstrap of the generic deployment helper (re-run to update it; idempotent):
#   sudo bash deploy/install.sh
# - installs deploy/edge_deploy.py as /usr/local/sbin/edge-deploy (the previous copy is kept as a backup)
# - creates the restricted account edge-deploy (no password, no shell for anything but the helper)
# - gives every runner user (edge-ci = renecaldeira, edge-ci-fernando = fespadoni) its own SSH key, accepted
#   only from 127.0.0.1 and only to run the helper for that owner
# - creates /etc/edge-deploy/apps (root-owned app configurations) and /var/lib/edge-deploy (state)
# Runner slots run with NoNewPrivileges, so a job reaches the helper over SSH to localhost, never through sudo.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root' >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
declare -A RUNNERS=([edge-ci]=renecaldeira [edge-ci-fernando]=fespadoni)

python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$here/edge_deploy.py"
if [[ -f /usr/local/sbin/edge-deploy ]]; then
  cp -p /usr/local/sbin/edge-deploy "/usr/local/sbin/edge-deploy.bak-$(date +%Y%m%d-%H%M%S)"
fi
install -o root -g root -m 0755 "$here/edge_deploy.py" /usr/local/sbin/edge-deploy
install -d -o root -g root -m 0755 /etc/edge-deploy /etc/edge-deploy/apps
install -d -o root -g root -m 0700 /var/lib/edge-deploy

if ! id edge-deploy >/dev/null 2>&1; then
  useradd --create-home --home-dir /home/edge-deploy --user-group --shell /bin/sh edge-deploy
  passwd -l edge-deploy >/dev/null
fi
chown root:root /home/edge-deploy; chmod 0755 /home/edge-deploy
install -d -o root -g root -m 0755 /home/edge-deploy/.ssh

sudoers="$(mktemp)"
{
  echo '# edge-deploy may run the helper only with an owner fixed by its SSH forced command'
  echo 'Defaults:edge-deploy env_keep += "SSH_ORIGINAL_COMMAND"'
  for owner in "${RUNNERS[@]}"; do echo "edge-deploy ALL=(root) NOPASSWD: /usr/local/sbin/edge-deploy ${owner}"; done
} > "$sudoers"
visudo -cf "$sudoers" >/dev/null
install -o root -g root -m 0440 "$sudoers" /etc/sudoers.d/edge-deploy
rm -f "$sudoers"

keys="$(mktemp)"
for runner in "${!RUNNERS[@]}"; do
  owner="${RUNNERS[$runner]}"
  home="$(getent passwd "$runner" | cut -d: -f6)"
  [[ -n "$home" ]] || { echo "Missing runner user $runner" >&2; exit 1; }
  install -d -o "$runner" -g "$runner" -m 0700 "$home/.ssh"
  if [[ ! -f "$home/.ssh/edge-deploy" ]]; then
    runuser -u "$runner" -- ssh-keygen -q -t ed25519 -N "" -C "$runner edge-deploy" -f "$home/.ssh/edge-deploy"
  fi
  printf 'restrict,from="127.0.0.1,::1",command="sudo -n /usr/local/sbin/edge-deploy %s" ssh-ed25519 %s %s\n' \
    "$owner" "$(cut -d' ' -f2 "$home/.ssh/edge-deploy.pub")" "$runner" >> "$keys"
  ssh-keyscan -t ed25519 127.0.0.1 2>/dev/null > "$home/.ssh/known_hosts_edge_deploy"
  chown "$runner:$runner" "$home/.ssh/known_hosts_edge_deploy"; chmod 0600 "$home/.ssh/known_hosts_edge_deploy"
done
install -o root -g root -m 0644 "$keys" /home/edge-deploy/.ssh/authorized_keys
rm -f "$keys"
visudo -c >/dev/null

echo "--- installed"
ls -l /usr/local/sbin/edge-deploy /etc/sudoers.d/edge-deploy /home/edge-deploy/.ssh/authorized_keys
cut -c1-80 /home/edge-deploy/.ssh/authorized_keys
ls /etc/edge-deploy/apps
# the helper refuses an unknown app: proves each key, the forced command and sudo work
for runner in "${!RUNNERS[@]}"; do
  home="$(getent passwd "$runner" | cut -d: -f6)"
  printf '%s: ' "$runner"
  runuser -u "$runner" -- ssh -T -i "$home/.ssh/edge-deploy" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o UserKnownHostsFile="$home/.ssh/known_hosts_edge_deploy" -o StrictHostKeyChecking=yes \
    edge-deploy@127.0.0.1 "deploy nothing-here ${RANDOM}" < /dev/null 2>&1 || true
done
