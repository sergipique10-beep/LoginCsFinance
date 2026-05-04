# Getting Started

## Prerequisites

- Python 3.10+
- Node.js 18+ and Angular CLI (`npm install -g @angular/cli`)
- [mkcert](https://github.com/FiloSottile/mkcert) for local HTTPS
- For Android builds: Android Studio + Capacitor CLI

---

## 1. Clone and install backend dependencies

```bash
git clone <repo-url>
cd LoginCsFinance
pip install -r requirements.txt
```

---

## 2. Generate local TLS certificates

Install mkcert once per machine:

```bash
# Windows — pick one
choco install mkcert
scoop install mkcert

# macOS
brew install mkcert
```

Install the local CA (once per machine) and generate the certificates:

```bash
mkcert -install

mkdir certs
mkcert -cert-file certs/localhost.pem -key-file certs/localhost-key.pem localhost 127.0.0.1
```

The `certs/` directory is git-ignored. Never commit certificate files.

---

## 3. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and set `JWT_SECRET` to a strong random value:

```bash
# Generate a 64-character secret (Linux/macOS/Git Bash)
python -c "import secrets; print(secrets.token_hex(32))"
```

See [configuration.md](./configuration.md) for all available variables.

> **Note:** `BASE_URL` must be publicly reachable for Steam's OpenID callback. Use [ngrok](https://ngrok.com/) in local dev: `ngrok http 8001` then set `BASE_URL=https://<your-subdomain>.ngrok-free.app`.

---

## 4. Start the backend

```bash
python run_dev.py
```

This starts uvicorn on `https://localhost:8001` using the certificates in `certs/`. Swagger UI is available at `https://localhost:8001/docs`.

---

## 5. Configure the Angular / Ionic project

The Angular project is at `C:\Users\Marc\Documents\CS-FINANCE\CS-FINANCE-ionic`.

**Angular 20, Ionic 8, and Capacitor 8 are already installed.** The steps below cover only what still needs to be done.

### 5a. Remove Firebase dependencies

```bash
cd C:\Users\Marc\Documents\CS-FINANCE\CS-FINANCE-ionic
npm uninstall @angular/fire
```

`firebase` es una dependencia transitiva de `@angular/fire`, no una directa. Se elimina automáticamente al desinstalar `@angular/fire`.

### 5b. Install the missing Capacitor Browser plugin

```bash
npm install @capacitor/browser
```

### 5c. Create the dev proxy

Create `C:\Users\Marc\Documents\CS-FINANCE\CS-FINANCE-ionic\proxy.conf.json`:

```json
{
  "/api": {
    "target": "https://localhost:8001",
    "secure": false,
    "pathRewrite": { "^/api": "" }
  }
}
```

`"secure": false` tells the Node proxy to accept the mkcert certificate without verifying it against the system trust store.

### 5d. Enable HTTPS in angular.json

Update the `serve` configuration:

```json
"serve": {
  "options": {
    "ssl": true,
    "sslCert": "../LoginCsFinance/certs/localhost.pem",
    "sslKey": "../LoginCsFinance/certs/localhost-key.pem"
  }
}
```

Or pass the flags directly at startup:

```bash
ng serve --ssl \
  --ssl-cert ../LoginCsFinance/certs/localhost.pem \
  --ssl-key ../LoginCsFinance/certs/localhost-key.pem \
  --proxy-config proxy.conf.json
```

Adjust the relative path to `certs/` if your Angular project lives elsewhere.

> **Note:** The proxy (`proxy.conf.json`) applies **only to `ng serve`** (development web). For native Android builds, HTTP calls go directly to `environment.apiUrl` — no proxy is involved. See [steam-auth-angular.md](./steam-auth-angular.md#ionic--capacitor-android) for environment configuration.

---

## 6. Verify

| URL | Expected |
|-----|----------|
| `https://localhost:4200` | Angular app loads without certificate warnings |
| `https://localhost:8001/docs` | FastAPI Swagger UI |
| `https://localhost:8001/` | `{"status": "ok"}` |

If the browser shows a certificate warning despite mkcert being installed, re-run `mkcert -install` and restart the browser.
