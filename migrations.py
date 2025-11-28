# the migration file is where you build your database tables
# If you create a new release for your extension ,
# remember the migration file is like a blockchain, never edit only add!

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
            wallet TEXT NOT NULL,
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