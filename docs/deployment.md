# Deployment

ChunkCanvas is a single process that serves the API and the built web app on one port
(8300 by default). Bind it to localhost and put HTTPS in front with a reverse proxy or tunnel.

The Linux bubblewrap backend requires working unprivileged user namespaces and
bubblewrap with `--disable-userns` (tested with 0.9.0). Startup runs the actual
sandbox command and fails if the host cannot enforce it. Parser children have
no host account database and cannot open additional user namespaces.

## Sizing

| Resource | Guidance |
| --- | --- |
| CPU | 4+ cores. Docling uses `CC_PARSER_THREADS` (default 4). |
| RAM | ~250 MB for the API plus up to ~2.5 GB per concurrent Docling parse (30 pages). 4 GB covers `CC_WORKER_CONCURRENCY=1`. |
| Disk | ~2 GB for the Python environment and ~750 MB for models. Uploaded data is deleted after the TTL. |
| GPU | Not used. |

Measured on the reference server (8-core AMD Ryzen 5 3550H laptop CPU, `CC_PARSER_THREADS=4`,
inside the bubblewrap sandbox): a 30-page PDF with tables parsed with the default hybrid parser
in **132 s**, and the service's cgroup peaked at **2.36 GB** (API, parser and page cache together).
A 20-page file took **92 s** / **2.0 GB**; most of the memory is the ~1.2 GB model load, plus about
40 MB per page. Docling-only took 138 s / 2.24 GB on the 30-page file; PyMuPDF-only takes a couple
of seconds. The public demo sets `CC_MAX_PAGES=20` with `CC_TRUNCATE_LONG_PDFS=true`: longer PDFs are
cut to their first 20 pages (inside the sandbox) and the editor says so, pointing to the repository.
The export keeps the original file's hash and `sourcePageCount`.
Requests never wait for parsing (the browser polls the job status), so
proxies with request time limits such as Cloudflare's ~100 s are fine.

## Bare metal with systemd (reference setup)

Follow the [Ubuntu installation steps](../README.md#install-on-ubuntu) to prepare the
checkout, converter, models and web build. Before starting a public instance, set the
host names, proxy and absolute paths for your deployment in the service environment file.

```ini
CC_PUBLIC_ORIGIN=https://chunkcanvas.example.com
CC_ALLOWED_HOSTS=chunkcanvas.example.com,127.0.0.1,localhost
CC_TRUST_PROXY_HEADERS=true
# Cloudflare Tunnel; use x-forwarded-for for nginx/Caddy.
CC_PROXY_IP_HEADER=cf-connecting-ip
CC_PARSER_SANDBOX=bwrap
# Use absolute paths for systemd.
CC_DATA_DIR=/srv/chunkcanvas/data
CC_WEB_DIST=/srv/chunkcanvas/apps/web/dist
CC_SOFFICE=/srv/chunkcanvas/.tools/libreoffice-26.2.7.3/opt/libreoffice26.2/program/soffice
```

Then install the user service (it needs `loginctl enable-linger $USER` to run without a login):

```bash
scripts/install-service.sh            # PORT=8300 by default
systemctl --user status chunkcanvas
journalctl --user -u chunkcanvas -f
```

The unit (`deploy/systemd/chunkcanvas.service`) caps memory at 4 GB without swap, sets
`NoNewPrivileges`, and starts uvicorn with `--no-proxy-headers` so the app alone decides whether
to trust forwarded client addresses.

Prepare updates in a separate checkout: install the lockfile, build the web app, and run the
tests there. Parser subprocesses read code from disk for every job, so changing the active
checkout while the API or a batch server is running can mix releases within one job. Before
switching releases, drain/stop every service using that checkout and take a consistent backup
of the database, document data, gallery and built assets. Start the services against the prepared
release and check opening, saving and exporting a document. Keep the previous release and its
matching data/assets as the rollback point. A small verified fix can be released independently;
there is no need to wait for unrelated parser changes.

### Updating a privately unpacked LibreOffice

Keep each release in a separate directory. The unpacked path inside the release
can stay the same across updates (both 26.2.6 and 26.2.7 use
`opt/libreoffice26.2`). Extracting over a running installation mixes old and
new files. For the November 2026 advisories the corrected release on the 26.2
branch is 26.2.7. Check the version using the exact executable selected by
`CC_SOFFICE`. [LibreOffice security advisories](https://www.libreoffice.org/about-us/security/advisories/)

On Ubuntu 24.04 x86-64, install the [host packages from step 1](../README.md#install-on-ubuntu)
before unpacking the vendor build. Extracting the DEBs with `dpkg-deb -x` leaves the
converter's system libraries for APT to install. Start in the repository root. The commands
check the vendor checksum and unpack into a new version-specific directory. Starting
with an existing release directory stops the extraction.

```bash
(
  set -eu
  release=26.2.7
  install_dir="$PWD/.tools/libreoffice-26.2.7.3"
  download_dir=$(mktemp -d)
  cd "$download_dir"
  archive="LibreOffice_${release}_Linux_x86-64_deb.tar.gz"
  url="https://download.documentfoundation.org/libreoffice/stable/$release/deb/x86_64/$archive"
  curl --fail --location --output "$archive" "$url"
  curl --fail --location --output SHA256SUMS "$url.sha256"
  sha256sum --check SHA256SUMS
  tar -xzf "$archive"
  mkdir -p "$(dirname "$install_dir")"
  mkdir -- "$install_dir"
  for pkg in LibreOffice_*/DEBS/*.deb; do dpkg-deb -x "$pkg" "$install_dir"; done
  "$install_dir/opt/libreoffice26.2/program/soffice" --version
)
```

Check that the result reports 26.2.7.3. Keep the previous version available when
testing a later build and update the absolute `CC_SOFFICE` path for each release.

Before switching, back up the current service configuration in a private
directory and keep the old executable and its complete installation. Test the
new executable on a separate port. For a site with Office temporarily disabled,
save the configuration **including** the disabling comment and missing
`CC_SOFFICE` path. Replace those two lines with one absolute `CC_SOFFICE` path
to the verified new executable. If the unit has a filesystem allowlist, bind the
new release read-only and keep the rollback version available to the installer.
Restart the service and check its configuration, a real Office upload, a PDF
upload and gallery opening. A fully enabled installation offers 41 formats.
The temporary Office-disabled configuration offers eight and still serves the
gallery.

Prepare a single rollback command or script before switching. It restores the
saved environment and unit file, reloads the user manager when the unit has
changed, and restarts the service. Verify the advertised format count matches
the saved configuration. Restoring the pre-update Office-disabled state keeps
the old executable on disk without exposing it to public uploads.

## HTTPS in front

`CC_TRUST_PROXY_HEADERS=true` makes rate limits use only `CC_PROXY_IP_HEADER` (default
`cf-connecting-ip`); there is no fallback to another header. The proxy must replace the selected
header rather than pass through a visitor-supplied value. Only
enable it when the port is reachable through the proxy alone (bind to 127.0.0.1). With
`CC_PUBLIC_ORIGIN` set, a request forwarded with `X-Forwarded-Proto: http` is redirected to HTTPS.

### Cloudflare Tunnel

In Zero Trust → Networks → Tunnels, add a public hostname for your tunnel with service
`HTTP` → `127.0.0.1:8300`. If your zone injects Cloudflare Web Analytics, also set
`CC_CSP_ALLOW_CLOUDFLARE_INSIGHTS=true` so the Content-Security-Policy allows its beacon.

### nginx

Set `CC_PROXY_IP_HEADER=x-forwarded-for`.

```nginx
server {
    listen 443 ssl;
    server_name chunkcanvas.example.com;
    # ssl_certificate ... ;
    client_max_body_size 11m;
    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header CF-Connecting-IP "";
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

### Caddy

Set `CC_PROXY_IP_HEADER=x-forwarded-for`.

```caddy
chunkcanvas.example.com {
    reverse_proxy 127.0.0.1:8300
}
```

## Operations

- **Retention**: `CC_DOCUMENT_TTL_SECONDS` (default 3600). Expired documents, stray directories
  and abandoned uploads are removed every 30 seconds.
- **Abuse limits**: `CC_UPLOADS_PER_CLIENT_PER_HOUR`, `CC_MAX_ACTIVE_DOCUMENTS_PER_CLIENT`,
  `CC_REQUESTS_PER_CLIENT_PER_MINUTE`, `CC_MAX_QUEUED_JOBS`. Limits are in memory and reset on
  restart. Capacity reservations and document counts are transactional in SQLite; sample copies
  have their own `CC_MAX_SAMPLE_DOCUMENTS_PER_CLIENT` cap, and `CC_MAX_STORED_DOCUMENTS` caps total
  retained documents. Sample copies additionally share `CC_MAX_STORED_SAMPLE_DOCUMENTS` (200 by
  default, capped at half the total), leaving storage available for uploads even across rotating
  client addresses. `CC_INSPECTION_CONCURRENCY` bounds inspection processes.
- **Uploads**: by default at most 32 requests may receive a body at once, two per client
  (`CC_MAX_UPLOADS_IN_PROGRESS_PER_CLIENT`, an IPv6 `/56`) and four per network
  (`CC_MAX_UPLOADS_IN_PROGRESS_PER_NETWORK`, an IPv6 `/48` or one IPv4 address).
  `CC_UPLOAD_IDLE_SECONDS` bounds each pause. After `CC_UPLOAD_GRACE_SECONDS` (10 seconds),
  `CC_UPLOAD_MIN_BYTES_PER_SECOND` requires an average 4 KiB/s, or the rate needed to finish a
  declared `Content-Length` in time if higher; sending occasional bytes cannot retain a slot until
  the 120-second total timeout. Capacity refusals do not consume hourly quota.
- **Logs** contain parser names, durations and error codes, never document text or file names.
  Each document directory keeps its parser's stderr (`job-stderr.log`) until the document expires.
- **Samples** are parsed once at startup with the default parser and cached in
  `data/sample-cache`, so "try a sample" opens instantly.

## Reproducing the Python environment

The Ubuntu installation uses `services/api/requirements.lock`, including CPU PyTorch and
pyhwp. `requirements.txt` declares direct runtime dependencies; it is not the installation lock.
Install the pinned `requirements-build.lock` first, then install the runtime lock with
`--no-build-isolation`. Both locks use direct official artifact URLs and SHA-256 checks, with
index lookup disabled. The runtime lock targets Linux x86-64 / CPython 3.12. Source archives
are verified, while their locally built wheels are not claimed to be byte-identical.

After a deliberate dependency update, run the tests in a separate candidate environment:

```bash
services/api/.venv/bin/python scripts/lock_requirements.py \
  --output /tmp/requirements.lock.new --build-output /tmp/requirements-build.lock.new
```

Review both files, validate a fresh installation, and then replace the tracked locks. The
generator checks installed runtime dependency constraints and excludes development-only tools.

### Filesystem of the bare-metal service

The installer supports a systemd user manager (tested with 255), Linux with
unprivileged user namespaces, and bubblewrap with nested sandbox support. It
requires these even when the inner parser uses another isolation mode. The user
manager reads the environment file. Before opening the API socket, the launcher
binds only the application module, virtualenv, configured models, converter,
enabled samples, gallery and web build read-only. Only the configured data
directory is persistent and writable. Temporary API files go there too.
The service has its own PID namespace. Its network remains available for the
configured model provider. The parser retains its separate network isolation.
An unavailable required mount, namespace or executable fails startup.

Use absolute configured paths. A private LibreOffice install must contain
`program/soffice`. Restart after changing paths so the launcher can bind the
new directories. Keep any previous unit and configuration before installing the
new unit. On rollback restore the saved unit, run `systemctl --user daemon-reload`
and restart the named service. The default unit name is `chunkcanvas`; use
`UNIT_NAME` and `PORT` for an independent installation.
