# Monzo backend

A self-hosted personal finance app with a FastAPI backend, PostgreSQL storage and a Next.js dashboard for Monzo transactions, categories, balances and budgets.

## Run a local dashboard

Install Docker with Compose, Python 3 and OpenSSL, then run `sh scripts/local-dashboard.sh`. Open `https://localhost:39475/spending`. The script creates a self-signed certificate and writes the login password to `secrets/local-dashboard/dashboard-login.txt`. The local database starts empty. Run `sh scripts/local-dashboard.sh stop` to stop the services.

## Configuration

The local launcher generates database credentials, API tokens and an encryption key under the Git-ignored `secrets/` directory. The root `.env.example` lists application settings. To connect a bank account, configure your own Monzo OAuth client and registered redirect URI. The example hostname under `example.invalid` is reserved for documentation.

To prepare the optional n8n alert workflow, copy `workflows/.env.example` to a private `.env.alerts` file in the repository root and set the sender and recipient. With Node 20.6 or later, run the following command from the repository root.

```sh
mkdir -p secrets
(umask 077 && node --env-file=.env.alerts scripts/render-email-workflow.mjs > secrets/monzoEmailAlerts.json)
```

Import the generated JSON into n8n, select your own saved SMTP and backend credentials, and publish the workflow.
