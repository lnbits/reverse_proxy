
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
            customer_label TEXT NOT NULL,
            subdomain TEXT NOT NULL,
            remote_port INT NOT NULL,
            ssh_user TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            public_id TEXT NOT NULL,
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
        CREATE TABLE wallet.extension_settings (
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