from datetime import datetime, timezone

from lnbits.db import FilterModel
from pydantic import BaseModel, Field


########################### Proxies ############################
class CreateProxies(BaseModel):
    customer_label: str
    subdomain: str
    remote_port: int
    ssh_user: str
    expires_at: datetime
    status: str
    wallet: str
    


class Proxies(BaseModel):
    id: str
    user_id: str
    customer_label: str
    subdomain: str
    remote_port: int
    ssh_user: str
    expires_at: datetime
    status: str
    wallet: str
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProxiesFilters(FilterModel):
    __search_fields__ = [
        "customer_label","subdomain","remote_port","ssh_user","expires_at","status","wallet",
    ]

    __sort_fields__ = [
        "customer_label",
        "subdomain",
        "remote_port",
        "ssh_user",
        "expires_at",
        "status",
        "wallet",
        
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
    customer_label: str          # whatever identifier: “Ben home node”, pubkey, etc.
    days: int = Field(gt=0)      # how many days to buy


class CreateTunnelResponse(BaseModel):
    tunnel_id: str
    subdomain: str
    remote_port: int
    ssh_user: str
    ssh_host: str
    ssh_command: str
    public_url: str
    expires_at: datetime
    payment_hash: str
    payment_request: str
