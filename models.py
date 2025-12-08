from datetime import datetime, timezone

from lnbits.db import FilterModel
from pydantic import BaseModel, Field

############################ Settings #############################

class ExtensionSettings(BaseModel):
    wallet: str
    public_id: str | None
    domain: str = "lnbitsproxy.com"
    ssh_user: str = "tunneluser"
    ssh_host: str = "lnbitsproxy.com"
    caddy_tunnels_file: str = "/etc/caddy/tunnels.caddy"
    remote_port_min: int = 12000
    remote_port_max: int = 13000
    price_per_day_sats: int = 100

    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def is_admin_only(cls) -> bool:
        return bool("False" == "True")


class UserExtensionSettings(ExtensionSettings):
    id: str

########################### Proxies ############################
class CreateProxies(BaseModel):
    subdomain: str
    remote_port: int
    ssh_user: str
    ssh_public_key: str | None = None
    expires_at: datetime
    status: str
    public_id: str
    client_note: str | None = None
    


class Proxies(BaseModel):
    id: str
    user_id: str
    subdomain: str
    remote_port: int
    ssh_user: str
    ssh_public_key: str | None = None
    expires_at: datetime
    status: str
    public_id: str
    client_note: str | None = None
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProxiesFilters(FilterModel):
    __search_fields__ = [
        "subdomain","remote_port","ssh_user","expires_at","status","public_id","client_note",
    ]

    __sort_fields__ = [
        "subdomain",
        "remote_port",
        "ssh_user",
        "expires_at",
        "status",
        "public_id",
        "client_note",
        
        "created_at",
        "updated_at",
    ]

    created_at: datetime | None
    updated_at: datetime | None


################################# Payments ###########################


class CreatePayments(BaseModel):
    tunnel_id: str
    invoice_id: str
    amount: int
    paid: bool
    


class Payments(BaseModel):
    id: str
    proxies_id: str
    tunnel_id: str
    invoice_id: str
    amount: int
    paid: bool
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PaymentsPaymentRequest(BaseModel):
    payments_id: str
    payment_hash: str | None = None
    payment_request: str | None = None


class PublicTopupRequest(BaseModel):
    days: int = Field(gt=0)




class PaymentsFilters(FilterModel):
    __search_fields__ = [
        "tunnel_id","invoice_id","amount","paid",
    ]

    __sort_fields__ = [
        "tunnel_id",
        "invoice_id",
        "amount",
        "paid",
        
        "created_at",
        "updated_at",
    ]

    created_at: datetime | None
    updated_at: datetime | None

########################### Tunnel Request/Response ############################

class CreateTunnelRequest(BaseModel):
    public_id: str
    days: int = Field(gt=0)
    client_note: str | None = None


class CreateTunnelResponse(BaseModel):
    tunnel_id: str
    subdomain: str
    remote_port: int
    ssh_user: str
    ssh_host: str
    ssh_private_key: str | None = None
    ssh_command: str
    public_url: str
    expires_at: datetime
    payment_hash: str
    payment_request: str
    client_note: str | None = None


class PublicTopupRequest(BaseModel):
    days: int = Field(gt=0)


class PublicTunnel(BaseModel):
    tunnel_id: str
    subdomain: str
    remote_port: int
    ssh_user: str
    ssh_host: str
    public_url: str
    expires_at: datetime
    status: str
    client_note: str | None = None
