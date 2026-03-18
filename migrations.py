
empty_dict: dict[str, str] = {}

async def m002_proxies(db):
    """
    Initial proxies table.
    """

    await db.execute(
        f"""
        CREATE TABLE reverse_proxy.proxies (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            subdomain TEXT NOT NULL,
            remote_port INT NOT NULL,
            ssh_user TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            public_id TEXT NOT NULL,
            client_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """
    )


async def m003_payments(db):
    """
    Initial payments table.
    """

    await db.execute(
        f"""
        CREATE TABLE reverse_proxy.payments (
            id TEXT PRIMARY KEY,
            proxies_id TEXT NOT NULL,
            tunnel_id TEXT NOT NULL,
            invoice_id TEXT NOT NULL,
            amount INT NOT NULL,
            paid BOOLEAN NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """
    )


async def m004_extension_settings(db):
    """
    Initial settings table.
    """

    await db.execute(
        f"""
        CREATE TABLE reverse_proxy.extension_settings (
            id TEXT NOT NULL,
            public_id TEXT,
            wallet NOT NULL,
            price_per_day_sats INT NOT NULL,
            domain TEXT NOT NULL,
            ssh_user TEXT NOT NULL,
            ssh_host TEXT NOT NULL,
            caddy_tunnels_file TEXT NOT NULL,
            remote_port_min INT NOT NULL,
            remote_port_max INT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """
    )


async def m005_add_ssh_public_key(db):
    """
    Store the client's SSH public key so it can be re-authorized after grace period.
    """

    await db.execute(
        """
        ALTER TABLE reverse_proxy.proxies
        ADD COLUMN ssh_public_key TEXT;
    """
    )


async def m006_dedupe_active_proxies_and_add_unique_indexes(db):
    """
    Collapse live subdomain/port collisions and enforce uniqueness for active/pending
    proxies so new tunnel reservations cannot overlap.
    """

    rows = await db.fetchall(
        """
        SELECT id, subdomain, remote_port, status, expires_at, updated_at, created_at
        FROM reverse_proxy.proxies
        WHERE status IN ('active', 'pending')
        ORDER BY expires_at DESC, updated_at DESC, created_at DESC, id DESC
        """
    )

    seen_subdomains: set[str] = set()
    seen_ports: set[int] = set()
    for row in rows:
        subdomain = row["subdomain"]
        remote_port = int(row["remote_port"])
        if subdomain in seen_subdomains or remote_port in seen_ports:
            await db.execute(
                f"""
                UPDATE reverse_proxy.proxies
                SET status = 'expired', updated_at = {db.timestamp_now}
                WHERE id = :id
                """,
                {"id": row["id"]},
            )
            continue
        seen_subdomains.add(subdomain)
        seen_ports.add(remote_port)

    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS reverse_proxy.active_subdomain_idx
        ON proxies (subdomain)
        WHERE status IN ('active', 'pending');
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS reverse_proxy.active_remote_port_idx
        ON proxies (remote_port)
        WHERE status IN ('active', 'pending');
        """
    )
