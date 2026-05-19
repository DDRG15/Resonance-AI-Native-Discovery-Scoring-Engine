# cookies/ — Session Cookie Vault

This directory stores exported browser session cookies for login-gated job boards.

**All `*.json` files here are gitignored and NEVER committed to GitHub.**

---

## Supported Boards

| Board | File | Why needed |
|-------|------|------------|
| wellfound.com | `wellfound.com.json` | Login wall — returns 0 jobs without auth |
| arc.dev | `arc.dev.json` | Login wall — returns 0 jobs without auth |
| welcometothejungle.com | `welcometothejungle.com.json` | Login + bot detection |

---

## How to Export Cookies from Chrome

### Method A — Chrome DevTools (no extension)

1. Log into the target site in Chrome
2. Open DevTools (F12) → **Application** tab → **Cookies** → click the site URL
3. Select all rows (Ctrl+A), right-click → **Copy all as JSON**
4. Paste into a file named `<domain>.json` in this directory

### Method B — EditThisCookie Extension (easier)

1. Install [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)
2. Navigate to the target site while logged in
3. Click the EditThisCookie icon → **Export** (clipboard icon)
4. Paste into `<domain>.json` in this directory

---

## Expected Format

JSON array of cookie objects (Chrome DevTools format):

```json
[
  {
    "name": "session_token",
    "value": "eyJ...",
    "domain": ".wellfound.com",
    "path": "/",
    "expirationDate": 1748000000.0,
    "httpOnly": true,
    "secure": true,
    "sameSite": "Lax"
  }
]
```

GEMA's `CookieVault` handles both Chrome DevTools format (`expirationDate`) and
Playwright format (`expires`) automatically.

---

## Verification

After placing a cookie file, verify it loads correctly:

```bash
cd gema-v2.1-extraction/gema
python -c "
from integrations.cookie_vault import CookieVault
v = CookieVault()
print('Domains with cookies:', v.list_domains())
print('wellfound has cookies:', v.has_cookies('wellfound.com'))
"
```
