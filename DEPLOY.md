# Deploying to a VPS (shadow mode, 24/7)

End state: a ~$6/month Ubuntu server running **two** services that survive
reboots and crashes independently:

- `alpaca-agent`     — the trading loop (shadow mode, places no orders)
- `alpaca-dashboard` — the web UI at `http://YOUR_SERVER_IP:8080`

Everything below is copy-paste. Estimated time: ~30 minutes.

---

## Part A — Put the code on GitHub (private)

1. Create a GitHub account if you don't have one: <https://github.com/signup>
2. New repo: <https://github.com/new>
   - Name: `alpaca-agent`
   - **Private** (important — this repo will hold no secrets, but keep it private)
   - Do **not** add a README/gitignore/license (the repo already has them)
   - Click **Create repository**
3. On your Mac, push the existing project to it. Copy the repo's SSH URL from
   the GitHub page (looks like `git@github.com:YOURNAME/alpaca-agent.git`), then:

   ```bash
   cd ~/alpaca-agent
   git branch -M main
   git remote add origin git@github.com:YOURNAME/alpaca-agent.git
   git push -u origin main
   ```

   If `git push` asks about SSH keys / permission denied, use the HTTPS URL
   instead (`https://github.com/YOURNAME/alpaca-agent.git`) — GitHub will prompt
   for your username and a Personal Access Token (create one at
   Settings → Developer settings → Personal access tokens).

`.env` and `data/` are gitignored — your keys never leave your Mac in this step.

---

## Part B — Create the server

Using **DigitalOcean** (Hetzner works the same; any Ubuntu 22.04/24.04 VPS is fine):

1. Sign up: <https://www.digitalocean.com/>
2. **Create → Droplets**
   - Region: closest to you (e.g. London)
   - Image: **Ubuntu 24.04 (LTS) x64**
   - Size: **Basic → Regular → $6/mo** (1 GB RAM / 1 CPU) — the $4 option is
     too small for the Python data libraries
   - Authentication: **SSH Key** (recommended) or Password
     - SSH key: on your Mac run `cat ~/.ssh/id_ed25519.pub` (or
       `ssh-keygen -t ed25519` first if you have none), paste the output
   - Hostname: `alpaca`
   - **Create Droplet**
3. Note the droplet's **public IP** (e.g. `203.0.113.45`).
4. Connect from your Mac's terminal:

   ```bash
   ssh root@203.0.113.45
   ```

   Type `yes` to trust it. You're now on the server.

---

## Part C — Bootstrap and run the setup script

You're SSH'd in as `root`. Replace `YOURNAME` with your GitHub username in every
command below.

**C1 — make the deploy key** (paste the whole block):

```bash
adduser --system --group --home /home/alpaca --shell /bin/bash alpaca 2>/dev/null || true
install -d -o alpaca -g alpaca -m 700 /home/alpaca/.ssh
sudo -u alpaca -H ssh-keygen -t ed25519 -N "" -f /home/alpaca/.ssh/id_ed25519 -C alpaca-deploy 2>/dev/null || true
sudo -u alpaca -H bash -c 'ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null'
echo; echo "=== ADD THIS AS A DEPLOY KEY ON YOUR REPO (read-only) ==="; cat /home/alpaca/.ssh/id_ed25519.pub; echo
```

**C2 — add that key to GitHub:** copy the `ssh-ed25519 ...` line it printed, then
GitHub → your repo → **Settings → Deploy keys → Add deploy key**:
- Title: `vps`
- Key: paste
- **Allow write access: leave unchecked**
- Add key

**C3 — clone and run setup** (paste the whole block):

```bash
sudo -u alpaca -H git clone git@github.com:YOURNAME/alpaca-agent.git /home/alpaca/alpaca-agent
bash /home/alpaca/alpaca-agent/deploy/setup.sh git@github.com:YOURNAME/alpaca-agent.git
```

This installs Python, builds the virtualenv, installs the two systemd services,
and opens the firewall (SSH + port 8080).

---

## Part D — Configure and start

The script created `/home/alpaca/alpaca-agent/.env` from the template. Edit it:

```bash
nano /home/alpaca/alpaca-agent/.env
```

Set these (leave everything else as default):

```
ALPACA_API_KEY=<your Alpaca PAPER key>
ALPACA_SECRET_KEY=<your Alpaca PAPER secret>
ALPACA_ENV=paper
EXECUTION_MODE=shadow
DASHBOARD_PASSWORD=<a long unique password — not one you use elsewhere>
```

Save: `Ctrl+O`, `Enter`, `Ctrl+X`.

Start both services:

```bash
systemctl start alpaca-agent alpaca-dashboard
systemctl status alpaca-agent alpaca-dashboard
```

Both should say `active (running)`. Press `q` to exit the status view.

---

## Part E — Verify

- **Dashboard:** open `http://YOUR_SERVER_IP:8080` in any browser. Log in with
  username `admin` and the password you set. Within ~5 minutes you'll see the
  first loop's decisions.
- **Agent log:**

  ```bash
  journalctl -u alpaca-agent -f
  ```

  You should see `---- loop 1 ----` and per-symbol decisions every 5 minutes.
  `Ctrl+C` to stop watching (the service keeps running).

---

## Operating it

| Task | Command (on the server) |
|---|---|
| **Halt trading immediately** | `touch /home/alpaca/alpaca-agent/KILL` — agent stops acting within one loop |
| **Resume** | `rm /home/alpaca/alpaca-agent/KILL` |
| Watch agent log | `journalctl -u alpaca-agent -f` |
| Watch dashboard log | `journalctl -u alpaca-dashboard -f` |
| Restart a service | `systemctl restart alpaca-agent` |
| Stop / start | `systemctl stop alpaca-agent` · `systemctl start alpaca-agent` |
| Check status | `systemctl status alpaca-agent alpaca-dashboard` |
| **Deploy an update** | on your Mac: `git push` — then on the server (as root): `bash /home/alpaca/alpaca-agent/deploy/setup.sh git@github.com:YOURNAME/alpaca-agent.git` (pulls + reinstalls + restarts) |
| Change config | `nano /home/alpaca/alpaca-agent/.env` then `systemctl restart alpaca-agent alpaca-dashboard` |

The two services are independent — restarting or crashing one does not affect
the other. Both restart automatically on failure and on server reboot.

---

## Later — going live (step 4)

Do **not** do this until you've reviewed shadow mode and told the assistant to
proceed. When you do, it's a config change in `.env`:

```
EXECUTION_MODE=live        # start submitting orders...
ALPACA_ENV=paper           # ...to the PAPER account first (still fake money)
```

then, only after that looks right, real money:

```
ALPACA_ENV=live
ALPACA_ALLOW_LIVE=true
```

followed by `systemctl restart alpaca-agent`. The risk caps in `.env`
(`MAX_POSITION_NOTIONAL_USD`, `MAX_TOTAL_ALLOCATION_USD`, `MAX_DAILY_LOSS_USD`)
are what limit your exposure — set them deliberately before that switch.
