# Reverse Proxy Extension

## Private LNbits Inc extension for offering paid reverse proxies

Creates Caddy entries and issues locked down keys for easy peasy reverse proxies.
To be used with the TunnelMeOut extension, or on lnpro.xyz

### Install Caddy

`sudo nano /etc/caddy/Caddyfile`

And add:

```
{
  email ben@lnbits.com
}

lnbits.lnpro.xyz {
  reverse_proxy 127.0.0.1:5000
}

import /etc/caddy/tunnels.caddy
```

Create the Caddyfile we will add routes to:

`touch /etc/caddy/tunnels.caddy`

`sudo systemctl reload caddy`

### How the two extensions work

Tunnel Me Out requests a reservation and Lightning invoice from
`POST /reverse_proxy/api/v1/tunnels`. The server reserves a subdomain, a loopback
port and a separate SSH key pair. Payment credits the reservation's expiry, installs
the restricted public key and rebuilds the managed Caddy routes. The client stores
the private key and runs SSH reverse forwarding back to its local LNbits port.
Renewals use `PUT /reverse_proxy/api/v1/payments/public/{tunnel_id}` and retain the
same key, port and subdomain. A new setup request creates a separate reservation;
`client_note` does not authorize replacing another tunnel's key.

The client polls `GET /reverse_proxy/api/v1/tunnels/{tunnel_id}/status?payment_hash=...`.
This endpoint verifies the invoice against the receiving wallet and reservation,
recovers missed settlement events, and reports server expiry and provisioning
readiness. Invoice credit and the paid flag commit together. Repeated callbacks,
status checks and reconnects do not add time again. The server also reconciles
payments and configuration every minute. Expired and unpaid reservations are
pruned after seven days; retained reservations keep their port and subdomain.

### Host requirements and upgrade

Upgrade **Reverse Proxy first**, then Tunnel Me Out. The client requires the new
status endpoint. Existing paid reservations keep their stored keys. The client
migration adds a nullable `last_error` column; the server needs no new migration.
Management routes now require the LNbits superuser because they configure local
files, SSH accounts and Caddy. Public invoice and status routes remain public.

Use OpenSSH with `restrict` and `permitlisten` support. For the dedicated tunnel
account, configure the following in `sshd_config` (replace `tunneluser` with the
configured account), validate with `sshd -t`, then reload SSH:

```text
Match User tunneluser
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    GatewayPorts no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
```

Each managed key permits only its assigned loopback listening port. Both explicit
`127.0.0.1` and the implicit `localhost` used by older clients are accepted.
`permitopen` is restricted to a non-resolving `.invalid` destination; the SSH
account's `AllowTcpForwarding remote` rule disables local forwarding entirely.
See [OpenSSH authorized_keys options](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT)
for the distinction between `permitlisten` and `permitopen`.

The account home is resolved through the system account database, not assumed to
be `/home/<user>`. The LNbits process must be able to update that account's
`.ssh/authorized_keys` **and its parent directory**. Files are replaced atomically,
preserving existing ownership and permissions, and protected by a sibling `.lock`
file. SSH still requires valid file ownership and StrictModes permissions: `.ssh`
should be `0700`, and `authorized_keys` should be `0600`, owned by the tunnel
account or root. Do not solve permission errors by making `.ssh` group/world
writable or disabling StrictModes. Running LNbits and the restricted SSH account
under the same Unix user avoids cross-account ownership problems; separate users
require an administrator-provisioned ownership/privilege arrangement.

Caddy's managed file and its directory must also be writable by LNbits, with the
file readable by Caddy. Prefer a dedicated directory (for example
`/var/lib/lnbits-proxy/`) over granting write access to all of `/etc/caddy`.
Update `caddy_tunnels_file` and the Caddyfile import to the same path. All settings
sharing that file are included when it is rebuilt. The extension calls
`sudo -n systemctl reload caddy`; allow that specific command without a password
for the service account. Reload failures remain retryable and are not reported as
successful provisioning.

### Diagnosing existing failures

- `Permission denied (publickey)`: check the assigned Unix account, file ownership,
  sshd logs, and the key stored in the proxy record. A client key previously lost
  by the old refresh bug cannot be recovered from its public key. If the private
  key still exists, derive its public key with `ssh-keygen -y -f <key-file>` and
  update that proxy's `ssh_public_key` through the authenticated management API.
  The next reconciliation installs it. Otherwise create a new reservation.
- `remote port forwarding failed`: check for another SSH process or manually
  copied connection script still owning that port. Use one connection per
  reservation. Port allocation now skips occupied local ports and reports
  exhaustion instead of looping indefinitely.
- Paid but not provisioned: check write permissions for both managed directories
  and the noninteractive Caddy reload command. Credit survives provisioning
  failure; retrying does not charge or extend twice.

Removing an authorized key prevents new logins; it does not disconnect an
already-authenticated SSH session. Expiry removes the Caddy route, and the managed
client stops its SSH process. Operators must terminate abandoned/manual sessions
if immediate server-side session revocation is required. Run LNbits with service
process-group cleanup (such as systemd's normal control-group stop behavior) so
an unclean application restart does not leave orphaned SSH clients.

### Regression checks

From the directory containing both repositories, using the Python environment
created from `reverse_proxy/uv.lock`:

```sh
DEBUG=false LNBITS_DATA_FOLDER=/tmp/lnproxfix-tests pytest --import-mode=importlib reverse_proxy/tests tunnel_me_out/tests
node --test tunnel_me_out/tests/test_ui.cjs
```

Tests use temporary databases and mocked invoices. The SSH integration test
starts a temporary loopback daemon and verifies forwarding plus rejection of an
unassigned port; it skips when `sshd` is unavailable. Production Lightning,
Caddy/TLS and PostgreSQL still require deployment validation.

### Paired local integration test

With both checkouts side by side, run this from their parent directory:

```sh
DEBUG=false LNBITS_DATA_FOLDER=/tmp/lnproxfix-tests uv run --project reverse_proxy --frozen --python 3.12 pytest --import-mode=importlib reverse_proxy/tests/test_paired_extensions.py -v
```

Install the OpenSSH client and server (`ssh`, `ssh-keygen`, and `sshd`) first.
The test skips if the sibling client checkout or `sshd` is unavailable.

This starts two temporary HTTP apps running the actual extension API routers,
separate migrated SQLite databases, and an isolated SSH daemon on random loopback
ports. It requests a tunnel through the client API, preserves the pending invoice
across refresh/repeated requests, simulates payment, forwards HTTP traffic through
SSH, renews without replacing the key, checks duplicate settlement and reconnect,
restores the client from its database after shutdown, and expires the tunnel.

The Lightning invoice backend, superuser authentication, and Caddy service reload
are simulated. Key generation, authorized-key restrictions, configuration-file
writes, HTTP requests between extensions, database migrations and settlement, and
SSH process management run for real. This is an extension integration test, not
two complete LNbits installations or a public HTTPS test. It does not contact the
hosted tunnel provider, spend sats, reload your services, or edit your normal SSH
files. Servers stop after the test; generated files stay in pytest's temporary test directory.


### Pydantic 2 compatibility

These checkouts target LNbits **1.7.0-rc1 or newer**, using Pydantic 2. Both
lockfiles use the same LNbits/Pydantic versions as the Tunnel Me Out migration.
Upgrade older LNbits installations before installing these extension versions.
The tunnel lifecycle changes remain in place; this compatibility update does not
require an additional database migration.

Model serialization and copies use `model_dump`, `model_dump_json`, and
`model_copy`. Optional fields explicitly default to `None`, following the
[Pydantic 2 migration guide](https://docs.pydantic.dev/latest/migration/).
Tests treat Pydantic deprecation warnings as errors, including the paired local
HTTP/SSH test, to prevent legacy model calls from being reintroduced.
