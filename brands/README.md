# Brand icons for HACS / Home Assistant

The icon shown in the **HACS catalog** and on the **Settings → Devices & Services**
page is **not** taken from this repository. Home Assistant and HACS load it from
the central brands CDN:

```
https://brands.home-assistant.io/_/difluid_microbalance/icon.png
```

To make our icon appear there, the images in
`custom_integrations/difluid_microbalance/` must be submitted to the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository.

## Files (already prepared here)

| File | Size | Purpose |
|---|---|---|
| `custom_integrations/difluid_microbalance/icon.png` | 256×256 | catalog / device page icon |
| `custom_integrations/difluid_microbalance/icon@2x.png` | 512×512 | hi-DPI icon |

Both are square PNGs whose content touches all four edges, satisfying the
brands "trimmed" CI check.

## How to submit

1. Fork <https://github.com/home-assistant/brands>
2. Copy this folder into the fork so the paths become:
   ```
   custom_integrations/difluid_microbalance/icon.png
   custom_integrations/difluid_microbalance/icon@2x.png
   ```
3. Commit and open a Pull Request.
4. The `hassfest`/brands CI runs automatically (checks size, square, PNG, trim).
5. After the PR is merged, the icon appears in HACS and HA within a day
   (CDN cache). You may need to restart HA / clear the browser cache.

> The `domain` **must** match the integration's `manifest.json` domain
> (`difluid_microbalance`), otherwise the icon will not be picked up.
