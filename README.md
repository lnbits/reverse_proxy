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