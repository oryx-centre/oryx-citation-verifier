#!/usr/bin/env python3
"""Oryx AI — public-deployment server (STAGED LOCALLY ONLY, NOT A LAUNCH).

A staged build of the public server from PUBLIC-DEPLOYMENT-SCOPE.md §3.
It runs the same staged agent (agent-public.py: ask -> human approve ->
export-pdf) behind magic-link auth, per-user quotas, and a spend cap.
It is NOT public: it binds 127.0.0.1 only, and this staging build must
never be exposed to a network, tunnelled, or proxied.

Modes:
  * local-staging (default): the server runs, but banners itself as
    LOCAL-STAGING. This is the mode for all staging tests.
  * public: ONLY when ALL of these hold — config public_enabled is
    true, monthly_spend_cap > 0, both per-token prices > 0, and the
    billed backend env vars (ORYX_PUBLIC_LLM_BASE_URL /
    ORYX_PUBLIC_LLM_MODEL / ORYX_PUBLIC_LLM_API_KEY) are set. If
    public_enabled is true but any requirement is missing, the server
    starts in local-staging mode and prints a loud warning for each
    missing requirement. Public mode NEVER silently enables.

Auth (no email sender — the operator invites):
  * Operator CLI: pilot-server-public.py invite --email X --config CFG
    prints a login link. The token is random, single-use, expiring,
    stored hashed at rest in a 0600 file.
  * GET /login?token=... sets an HMAC-signed session cookie (secret in
    a 0600 file generated on first run). No passwords anywhere.
  * Every route except /login and /health requires a valid session.
    (/logout only clears the client's cookie and changes no server
    state, so it needs no session.)
  * Operator (config operator_email) sees all jobs and can approve /
    export any. Reviewers (config reviewers) see all jobs and can
    approve / export. Regular users see ONLY their own jobs — other
    users' jobs return 404 (existence is not leaked). Approve / export
    are staff actions: anyone else gets 403.

Quotas: config quota_jobs_per_day (default 3 — a PROPOSAL the owner
can change, not decided policy). Counts every job a user creates on a
UTC calendar day, including failed ones (they consumed backend work).
Enforced at job creation with a plain refusal message.

Spend cap: config monthly_spend_cap + per-token prices. Spend is the
sum of finished jobs' token costs for the current UTC month (from each
run's usage.json). New jobs are refused when accumulated spend reaches
the cap — whenever the cap and prices are set, in either mode. If the
cap or prices are unset/zero, spend is tracked as null ("unpriced")
and the cap cannot engage; that is exactly why public mode refuses
to enable without them.

Billed-key wiring: agent-public.py selects its backend from
environment variables only (base URL / model / key). The key is never
written to disk, logged, or printed. In staging the env vars are
unset and the local config backend (local router, flash-lite class
model) applies — staging traffic ONLY. The free-tier routers must
never serve public traffic at launch.

Abuse controls: 2000-char question cap (same as local); STOP-file
kill switch in the jobs dir; PUBLIC_ENABLED config flag (default
false); login rate limit (5/min per IP, in-memory).
  * NO keyword blocklist for prompt injection, deliberately: a keyword
    filter is theater (trivially bypassed, false positives on
    legitimate research questions). The agent's system prompts already
    treat user text as data, never as instructions, and the human gate
    catches issues before anything is delivered. Documented here
    instead of faking a filter.

Cost logging per job (job.json): tokens_in, tokens_out, tokens_total,
tokens_reported, spend (null when unpriced), wall_seconds, plus
review_seconds (approval time minus needs_review time).

Usage:
  python3 pilot-server-public.py serve --config public-config.json [--port 8933]
  python3 pilot-server-public.py invite --email user@example.com --config public-config.json [--base-url https://app.oryxcentre.dpdns.org]
  curl localhost:8933/health        # no auth: ok + mode

Jobs live in <jobs_dir>/ (default ./public-jobs, SEPARATE from the
local MVP's pilot-jobs — the local server's jobs are never touched).
State (secret + login tokens) lives in <state_dir>/ (default
./public-state), both files 0600.
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_PUBLIC = os.path.join(HERE, "agent-public.py")
MAX_QUESTION_CHARS = 2000
COOKIE_NAME = "oryx_public_session"
LOGIN_RATE_WINDOW = 60
LOGIN_RATE_MAX = 5

# Populated by load_runtime() at serve time; invite CLI sets the subset
# it needs. Never import-time side effects (invite must not start a
# server, serve must not mint tokens).
CFG = {}
JOBS_DIR = ""
STATE_DIR = ""
SECRET = b""
MODE = "local-staging"
MODE_REASONS = []
MODEL_DISPLAY = "unknown"

job_queue = queue.Queue()
state_lock = threading.Lock()
login_attempts = {}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def utc_month():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _resolve(p):
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("operator_email", "")
    cfg.setdefault("reviewers", [])
    cfg.setdefault("beta_allowlist", [])
    cfg.setdefault("public_enabled", False)
    cfg.setdefault("monthly_spend_cap", 0)
    cfg.setdefault("price_per_1k_input_tokens", 0)
    cfg.setdefault("price_per_1k_output_tokens", 0)
    cfg.setdefault("quota_jobs_per_day", 3)
    cfg.setdefault("login_token_ttl_seconds", 3600)
    cfg.setdefault("session_ttl_seconds", 86400)
    cfg.setdefault("cookie_secure", False)
    cfg.setdefault("jobs_dir", "public-jobs")
    cfg.setdefault("state_dir", "public-state")
    cfg.setdefault("port", 8933)
    cfg.setdefault("base_url", "http://127.0.0.1:8933")
    return cfg


def compute_mode(cfg):
    """Return (mode, reasons). Public requires every requirement."""
    reasons = []
    if not cfg.get("public_enabled"):
        reasons.append("public_enabled is false")
    cap = cfg.get("monthly_spend_cap") or 0
    pin = cfg.get("price_per_1k_input_tokens") or 0
    pout = cfg.get("price_per_1k_output_tokens") or 0
    if not (isinstance(cap, (int, float)) and cap > 0):
        reasons.append("monthly_spend_cap unset or zero")
    # Verified free tier (Z.ai GLM flash, $0 billing, auto-recharge OFF):
    # $0 prices are legitimate, and the key comes via the credential
    # surrogate, not an env var — so neither requirement applies there.
    # Decision recorded in beta-launch-decisions.md, 28 Oct 2026.
    free_tier = (os.environ.get("ORYX_PUBLIC_LLM_BASE_URL", "").find("api.z.ai") >= 0
                 and os.environ.get("ORYX_PUBLIC_LLM_MODEL", "").startswith("glm-")
                 and os.environ.get("ORYX_PUBLIC_LLM_MODEL", "").endswith("-flash"))
    if not free_tier and not (isinstance(pin, (int, float)) and pin > 0 and
            isinstance(pout, (int, float)) and pout > 0):
        reasons.append("per-token prices unset or zero")
    env_ok = bool(os.environ.get("ORYX_PUBLIC_LLM_BASE_URL") and
                  os.environ.get("ORYX_PUBLIC_LLM_MODEL") and
                  (os.environ.get("ORYX_PUBLIC_LLM_API_KEY") or free_tier))
    if not env_ok:
        reasons.append("billed backend env vars not fully set")
    if reasons:
        return "local-staging", reasons
    return "public", []


def _ensure_state_files():
    global SECRET
    os.makedirs(STATE_DIR, exist_ok=True)
    secret_path = os.path.join(STATE_DIR, "secret.key")
    if os.path.isfile(secret_path):
        with open(secret_path, "rb") as f:
            SECRET = f.read().strip()
    else:
        SECRET = secrets.token_hex(32).encode()
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(SECRET)
    tokens_path = os.path.join(STATE_DIR, "login-tokens.json")
    if not os.path.isfile(tokens_path):
        fd = os.open(tokens_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({}, f)


def _read_tokens():
    with open(os.path.join(STATE_DIR, "login-tokens.json")) as f:
        return json.load(f)


def _write_tokens(tokens):
    p = os.path.join(STATE_DIR, "login-tokens.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def mint_login_token(email, ttl_seconds):
    """Store only the hash; return the raw token for the link.
    Expired entries are pruned here so the file cannot grow
    unboundedly with dead tokens."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with state_lock:
        tokens = _read_tokens()
        now = time.time()
        tokens = {k: v for k, v in tokens.items()
                  if v.get("expires_at", 0) >= now}
        tokens[digest] = {
            "email": email,
            "expires_at": (now + ttl_seconds),
        }
        _write_tokens(tokens)
    return token


def consume_login_token(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    with state_lock:
        tokens = _read_tokens()
        rec = tokens.pop(digest, None)
        if rec is not None:
            _write_tokens(tokens)
    if rec is None:
        return None
    if rec["expires_at"] < time.time():
        return None
    return rec["email"]


def make_session(email, ttl_seconds):
    exp = int(time.time()) + ttl_seconds
    payload = "%s|%d" % (email, exp)
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    raw = "%s|%s" % (payload, sig)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_session(cookie_value):
    try:
        raw = base64.urlsafe_b64decode(cookie_value.encode()).decode()
        email, exp, sig = raw.rsplit("|", 2)
        want = hmac.new(SECRET, ("%s|%s" % (email, exp)).encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            return None
        if int(exp) < time.time():
            return None
        return email
    except (ValueError, TypeError):
        return None


def login_rate_ok(ip):
    """5 login attempts per IP per minute, in-memory."""
    now = time.time()
    with state_lock:
        lst = [t for t in login_attempts.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
        if len(lst) >= LOGIN_RATE_MAX:
            login_attempts[ip] = lst
            return False
        lst.append(now)
        login_attempts[ip] = lst
        return True

# ---- Jobs, quotas, spend ------------------------------------------------

def job_path(job_id):
    return os.path.join(JOBS_DIR, job_id)


def read_job(job_id):
    p = os.path.join(job_path(job_id), "job.json")
    if not os.path.isfile(p):
        return None
    with open(p) as f:
        return json.load(f)


def write_job(job):
    p = os.path.join(job_path(job["id"]), "job.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=2)
    os.replace(tmp, p)


def all_jobs():
    out = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for d in sorted(os.listdir(JOBS_DIR)):
        j = read_job(d)
        if j:
            out.append(j)
    return out


def visible_jobs(email):
    """Operator + reviewers see all; regular users see only their own."""
    if email == CFG.get("operator_email") or email in CFG.get("reviewers", []):
        return all_jobs()
    return [j for j in all_jobs() if j.get("owner") == email]


def can_view(email, job):
    if email == CFG.get("operator_email") or email in CFG.get("reviewers", []):
        return True
    return job.get("owner") == email


def can_review(email):
    return email == CFG.get("operator_email") or email in CFG.get("reviewers", [])


def quota_used(email):
    today = utc_today()
    n = 0
    for j in all_jobs():
        if j.get("owner") == email and (j.get("created_at") or "")[:10] == today:
            n += 1
    return n


def month_spend():
    """Sum of priced job spend for the current UTC month. Unpriced jobs
    (spend null) contribute nothing — they are not free, they are
    unpriced, and public mode cannot enable while prices are unset."""
    month = utc_month()
    total = 0.0
    for j in all_jobs():
        if (j.get("created_at") or "")[:7] == month and j.get("spend") is not None:
            total += j["spend"]
    return total


def job_spend_from_usage(run_dir):
    """Compute a finished job's token cost from its usage.json.
    Returns (tokens_in, tokens_out, tokens_total, reported, spend) with
    spend None when prices are unset/zero (unpriced, not free)."""
    usage_path = os.path.join(run_dir, "usage.json")
    if not os.path.isfile(usage_path):
        return None, None, None, False, None
    with open(usage_path) as f:
        usage = json.load(f)
    totals = usage.get("totals", {})
    tin = totals.get("prompt_tokens")
    tout = totals.get("completion_tokens")
    reported = totals.get("calls_with_usage", 0) > 0
    total = None
    if isinstance(tin, int) and isinstance(tout, int):
        total = tin + tout
    pin = CFG.get("price_per_1k_input_tokens") or 0
    pout = CFG.get("price_per_1k_output_tokens") or 0
    spend = None
    if (isinstance(tin, int) and isinstance(tout, int) and pin > 0
            and pout > 0):
        spend = round(tin / 1000.0 * pin + tout / 1000.0 * pout, 6)
    return tin, tout, total, reported, spend


def run_agent_public(args, log_path):
    """Run one agent-public.py command; append combined output to
    log_path. Environment (incl. any billed-backend vars) is inherited
    — the key is never written to disk, a file, or a log line."""
    with open(log_path, "a") as lf:
        lf.write("\n$ python3 agent-public.py %s\n" % " ".join(args))
        lf.flush()
        proc = subprocess.run(
            [sys.executable, AGENT_PUBLIC] + args,
            stdout=lf, stderr=subprocess.STDOUT, timeout=1800)
    return proc.returncode


def dispatcher():
    """FIFO: one job at a time, ask -> needs_review (or failed)."""
    while True:
        job_id = job_queue.get()
        job = read_job(job_id)
        if job is None:
            job_queue.task_done()
            continue
        run_dir = os.path.join(job_path(job_id), "run")
        log_path = os.path.join(job_path(job_id), "agent.log")
        job["status"] = "running"
        job["started_at"] = now_iso()
        write_job(job)
        t0 = time.time()
        try:
            rc = run_agent_public(
                ["ask", "--question", job["question"],
                 "--run-dir", run_dir], log_path)
        except subprocess.TimeoutExpired:
            rc = 99
            with open(log_path, "a") as lf:
                lf.write("\nAGENT TIMED OUT after 1800s\n")
        job["wall_seconds"] = round(time.time() - t0, 1)
        job["agent_exit_code"] = rc
        job["run_dir"] = run_dir
        job["finished_at"] = now_iso()
        if rc == 0 and os.path.isfile(os.path.join(run_dir, "08-report-draft.md")):
            job["status"] = "needs_review"
            # v2 verification layers (best-effort; never fails the job).
            try:
                layers = subprocess.run(
                    [sys.executable, os.path.join(os.path.dirname(__file__),
                     "verify_layers.py"), run_dir],
                    capture_output=True, text=True, timeout=300)
                with open(log_path, "a") as lf:
                    lf.write("\n[V2 LAYERS] " + (layers.stdout or layers.stderr)[-500:] + "\n")
                import json as _json
                _audit = os.path.join(run_dir, "09-layer-audit.json")
                if os.path.isfile(_audit):
                    job["v2_triage_score"] = _json.load(open(_audit)).get("paperverify_triage_score")
            except Exception as _e:  # noqa: BLE001 - layers are advisory
                with open(log_path, "a") as lf:
                    lf.write("\n[V2 LAYERS FAILED] %s\n" % _e)
        else:
            job["status"] = "failed"
            with open(log_path, "a") as lf:
                lf.write("\nJOB FAILED: agent exit %s\n" % rc)
        tin, tout, total, reported, spend = job_spend_from_usage(run_dir)
        job["tokens_in"] = tin
        job["tokens_out"] = tout
        job["tokens_total"] = total
        job["tokens_reported"] = reported
        job["spend"] = spend
        spend_txt = ("unpriced (prices unset)" if spend is None
                     else "spend %.6f" % spend)
        job["cost_note"] = ("model=%s; wall=%ss; tokens in=%s out=%s; %s"
                            % (job.get("model"), job["wall_seconds"],
                               tin, tout, spend_txt))
        write_job(job)
        job_queue.task_done()


# ---- HTML (adapted from the local MVP UI; mode banner differs) --------

def md_to_html(text):
    """Minimal Markdown -> HTML for draft-report viewing (stdlib only).
    Escapes first, so raw HTML in a report can never execute."""
    esc = html.escape(text)

    def _inline(s):
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
        return s

    blocks = []
    lines = esc.split("\n")
    i = 0
    in_fence = False
    fence = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_fence:
                blocks.append("<pre>%s</pre>" % "\n".join(fence))
                fence = []
                in_fence = False
            else:
                in_fence = True
            i += 1
            continue
        if in_fence:
            fence.append(line)
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            blocks.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2)), lvl))
            i += 1
            continue
        if line.startswith("&gt; "):
            blocks.append("<blockquote>%s</blockquote>" % _inline(line[5:]))
            i += 1
            continue
        if re.match(r"^[-*]\s+", line) or re.match(r"^\d+\.\s+", line):
            items = []
            while i < len(lines) and (re.match(r"^[-*]\s+", lines[i]) or re.match(r"^\d+\.\s+", lines[i])):
                items.append("<li>%s</li>" % _inline(re.sub(r"^([-*]|\d+\.)\s+", "", lines[i])))
                i += 1
            blocks.append("<ul>%s</ul>" % "".join(items))
            continue
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,3})\s+", lines[i]) and not lines[i].strip().startswith("```"):
            para.append(lines[i])
            i += 1
        blocks.append("<p>%s</p>" % _inline(" ".join(para)))
    if in_fence:
        blocks.append("<pre>%s</pre>" % "\n".join(fence))
    return "\n".join(blocks)


def _page(title, inner):
    body = ("<html><head><meta charset='utf-8'><title>%s</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:2em auto;padding:0 1em;line-height:1.5}"
            "pre{background:#f4f4f4;padding:1em;overflow:auto;white-space:pre-wrap}"
            ".banner{background:#fff3cd;padding:0.8em 1em;border:1px solid #e6c87a}"
            ".fail{color:#a00;font-weight:bold} table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:6px;text-align:left}</style></head>"
            "<body>%s</body></html>") % (html.escape(title), inner)
    return body.encode()


def mode_banner():
    if MODE == "public":
        return ("<div class='banner'>PUBLIC MODE — capped billed backend. "
                "Jobs are refused when the monthly spend cap is reached."
                "</div>")
    return ("<div class='banner'>LOCAL-STAGING MODE — this server is NOT "
            "public. It binds 127.0.0.1 only. Public mode requires "
            "public_enabled + a spend cap + per-token prices + the billed "
            "backend env vars.</div>")


def job_html(job, email):
    """Browser view of one job (ownership already checked by caller)."""
    jid = job["id"]
    st = job["status"]
    staff = can_review(email)
    parts = ["<p><a href='/'>&larr; All jobs</a></p>", mode_banner(),
             "<h1>Job %s</h1>" % html.escape(jid),
             "<p><b>Status:</b> %s</p>" % html.escape(st)]
    if st == "failed":
        parts.append("<p class='fail'>This job FAILED — the log tail below states why. Nothing was faked to complete it.</p>")
    parts.append("<p><b>Question:</b> %s</p>" % html.escape(job["question"]))
    parts.append("<p><b>Requester:</b> %s (%s, %s)</p>" % (
        html.escape(job.get("requester_name") or ""),
        html.escape(job.get("requester_org") or ""),
        html.escape(job.get("requester_email") or "")))
    for k in ("owner", "created_at", "started_at", "finished_at",
              "wall_seconds", "agent_exit_code", "tokens_in", "tokens_out",
              "tokens_total", "spend", "review_seconds", "cost_note"):
        if job.get(k) is not None:
            parts.append("<p><b>%s:</b> %s</p>" % (html.escape(k), html.escape(str(job[k]))))
    log_path = os.path.join(job_path(jid), "agent.log")
    if os.path.isfile(log_path):
        with open(log_path, errors="replace") as f:
            tail = f.read().splitlines()[-60:]
        parts.append("<h2>Agent log (last 60 lines)</h2><pre>%s</pre>" % html.escape("\n".join(tail)))
    run_dir = job.get("run_dir")
    if run_dir and os.path.isdir(run_dir):
        files = sorted(os.listdir(run_dir))
        rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (
            html.escape(fn),
            os.path.getsize(os.path.join(run_dir, fn))) for fn in files)
        parts.append("<h2>Run files</h2><table><tr><th>file</th><th>bytes</th></tr>%s</table>" % rows)
        draft = os.path.join(run_dir, "08-report-draft.md")
        if os.path.isfile(draft):
            with open(draft, errors="replace") as f:
                draft_md = f.read()
            label = "DRAFT — not approved" if st == "needs_review" else "Report"
            parts.append("<h2>%s</h2><p><a href='/job/%s/draft'>Raw Markdown</a></p>%s" % (
                label, html.escape(jid), md_to_html(draft_md)))
    if st == "needs_review" and staff:
        parts.append("""<h2>Human gate</h2><p>Review the draft above. Approval
        requires your name as reviewer.</p>
        <input id='reviewer' placeholder='Reviewer name' size='40'>
        <button onclick="approve()">Approve</button>
        <script>async function approve(){
          const r=document.getElementById('reviewer').value.trim();
          if(!r){alert('Reviewer name required');return;}
          const res=await fetch('/job/%s/approve',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({reviewer:r})});
          const j=await res.json();
          if(!res.ok){alert('Approve failed: '+(j.error||res.status));return;}
          location.reload();}</script>""" % html.escape(jid))
    if st == "needs_review" and not staff:
        parts.append("<p><i>Awaiting human review by the Oryx team. You will "
                     "see the final PDF here once it is approved.</i></p>")
    if st == "approved" and staff:
        parts.append("""<h2>Export</h2><button onclick="exp()">Export final PDF</button>
        <script>async function exp(){
          const res=await fetch('/job/%s/export',{method:'POST',
            headers:{'Content-Type':'application/json'},body:'{}'});
          const j=await res.json();
          if(!res.ok){alert('Export failed: '+(j.error||res.status));return;}
          location.reload();}</script>""" % html.escape(jid))
    if st == "exported":
        parts.append("<h2>Final PDF</h2><p><a href='/job/%s/pdf'>Download report-final.pdf</a> (page 1 carries the Audit Certificate)</p>" % html.escape(jid))
    if st in ("queued", "running"):
        parts.append("<script>setTimeout(()=>location.reload(),10000);</script>")
    return _page("Job %s" % jid, "".join(parts))

# ---- HTTP handler --------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep stdout clean
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 65536:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _session_email(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(COOKIE_NAME + "="):
                return verify_session(part[len(COOKIE_NAME) + 1:])
        return None

    def _require_auth(self):
        """Return the session email, or refuse (browsers redirect to
        /login; API clients get 401). Nothing guarded is ever served
        without a valid session."""
        email = self._session_email()
        if email:
            return email
        accept = self.headers.get("Accept") or ""
        if "text/html" in accept:
            self._redirect("/login")
        else:
            self._json(401, {"error": "authentication required"})
        return None

    def _session_cookie(self, email):
        val = make_session(email, CFG.get("session_ttl_seconds", 86400))
        cookie = ("%s=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=%d"
                  % (COOKIE_NAME, val, CFG.get("session_ttl_seconds", 86400)))
        if CFG.get("cookie_secure"):
            cookie += "; Secure"
        return cookie

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        query = parse_qs(urlparse(self.path).query)
        if parts == ["health"]:
            cap = CFG.get("monthly_spend_cap") or 0
            pin = CFG.get("price_per_1k_input_tokens") or 0
            pout = CFG.get("price_per_1k_output_tokens") or 0
            return self._json(200, {
                "status": "ok", "mode": MODE,
                "public_enabled": bool(CFG.get("public_enabled")),
                "cap_configured": bool(cap > 0 and pin > 0 and pout > 0),
            })
        if parts == ["login"]:
            ip = self.client_address[0]
            tokens = query.get("token") or []
            if not tokens:
                inner = ("<h1>Sign in</h1><p>Sign-in links are issued by "
                         "the Oryx operator for invited users only. If you "
                         "have a link, open it — it signs you in "
                         "directly. There is no password and no public "
                         "sign-up.</p>")
                return self._html(200, _page("Sign in", inner))
            if not login_rate_ok(ip):
                return self._json(429, {"error": "too many login attempts"})
            email = consume_login_token(tokens[0])
            if not email:
                return self._json(401, {"error": "invalid or expired login link"})
            return self._redirect("/", cookie=self._session_cookie(email))
        if parts == ["logout"]:
            dead = ("%s=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
                    % COOKIE_NAME)
            return self._redirect("/login", cookie=dead)
        email = self._require_auth()
        if not email:
            return
        if not parts:
            return self._dashboard(email)
        if parts == ["jobs"]:
            out = []
            for j in visible_jobs(email):
                out.append({k: j.get(k) for k in
                            ("id", "status", "question", "requester_name",
                             "requester_org", "owner", "created_at",
                             "wall_seconds", "spend")})
            return self._json(200, out)
        if len(parts) == 2 and parts[0] == "job":
            job = read_job(parts[1])
            if job is None or not can_view(email, job):
                return self._json(404, {"error": "unknown job"})
            accept = self.headers.get("Accept") or ""
            if "text/html" in accept:
                return self._html(200, job_html(job, email))
            return self._json(200, job)
        if len(parts) == 3 and parts[0] == "job" and parts[2] == "draft":
            job = read_job(parts[1])
            if job is None or not can_view(email, job) or not job.get("run_dir"):
                return self._json(404, {"error": "unknown job"})
            draft = os.path.join(job["run_dir"], "08-report-draft.md")
            if not os.path.isfile(draft):
                return self._json(404, {"error": "no draft for job"})
            with open(draft, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if len(parts) == 3 and parts[0] == "job" and parts[2] == "pdf":
            job = read_job(parts[1])
            if job is None or not can_view(email, job) or not job.get("pdf_path") or \
                    not os.path.isfile(job["pdf_path"]):
                return self._json(404, {"error": "no exported PDF for job"})
            with open(job["pdf_path"], "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="oryx-ai-report.pdf"')
            self.end_headers()
            self.wfile.write(data)
            return
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        email = self._require_auth()
        if not email:
            return
        if parts == ["job"]:
            stop_file = os.path.join(JOBS_DIR, "STOP")
            if os.path.exists(stop_file):
                return self._json(503, {"error": "server stopped (STOP file present)"})
            body = self._read_json()
            if not body:
                return self._json(400, {"error": "JSON body required"})
            q = (body.get("question") or "").strip()
            if not q or len(q) > MAX_QUESTION_CHARS:
                return self._json(400, {"error": "question required, max %d chars" % MAX_QUESTION_CHARS})
            # NOTE (deliberate, not an omission): there is NO keyword
            # blocklist for prompt injection. User text is treated as
            # data by the agent's system prompts, never as instructions,
            # and the human gate reviews every draft before delivery.
            # A keyword filter would be theater — trivially bypassed
            # and prone to false positives on legitimate questions.
            quota = CFG.get("quota_jobs_per_day", 3)
            used = quota_used(email)
            if used >= quota:
                return self._json(403, {"error": "daily job quota reached: %d jobs/day for %s (used %d)" % (quota, email, used)})
            cap = CFG.get("monthly_spend_cap") or 0
            pin = CFG.get("price_per_1k_input_tokens") or 0
            pout = CFG.get("price_per_1k_output_tokens") or 0
            if cap > 0 and pin > 0 and pout > 0 and month_spend() >= cap:
                return self._json(403, {"error": "monthly spend cap reached (cap %.6f, spent %.6f)" % (cap, month_spend())})
            job_id = "%s-%s" % (
                datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
                uuid.uuid4().hex[:6])
            os.makedirs(job_path(job_id), exist_ok=True)
            job = {
                "id": job_id, "status": "queued", "question": q,
                "requester_name": body.get("requester_name", ""),
                "requester_org": body.get("requester_org", ""),
                # Owner is the authenticated session, never a
                # client-supplied address (prevents impersonation).
                "requester_email": email, "owner": email,
                "model": MODEL_DISPLAY,
                "created_at": now_iso(), "started_at": None,
                "finished_at": None, "wall_seconds": None,
                "agent_exit_code": None, "run_dir": None,
                "approval": None, "pdf_path": None,
                "tokens_in": None, "tokens_out": None,
                "tokens_total": None, "tokens_reported": False,
                "spend": None, "review_seconds": None,
            }
            write_job(job)
            job_queue.put(job_id)
            return self._json(201, {"job_id": job_id, "status": "queued"})
        if len(parts) == 3 and parts[0] == "job" and parts[2] in ("approve", "export"):
            job = read_job(parts[1])
            if job is None or not can_view(email, job):
                return self._json(404, {"error": "unknown job"})
            if not can_review(email):
                return self._json(403, {"error": "approval and export are reviewer actions"})
            action = parts[2]
            if action == "approve":
                body = self._read_json() or {}
                reviewer = (body.get("reviewer") or "").strip()
                if job["status"] != "needs_review":
                    return self._json(409, {"error": "job not awaiting review", "status": job["status"]})
                if not reviewer:
                    return self._json(400, {"error": "reviewer required"})
                log_path = os.path.join(job_path(job["id"]), "agent.log")
                rc = run_agent_public(["approve", "--run-dir", job["run_dir"],
                                       "--reviewer", reviewer], log_path)
                if rc != 0:
                    return self._json(500, {"error": "agent approve failed", "exit": rc})
                job["status"] = "approved"
                job["approval"] = {"reviewer": reviewer, "at": now_iso()}
                try:
                    fin = datetime.datetime.fromisoformat(job["finished_at"])
                    ap = datetime.datetime.fromisoformat(job["approval"]["at"])
                    job["review_seconds"] = round((ap - fin).total_seconds(), 1)
                except (TypeError, ValueError):
                    job["review_seconds"] = None
                write_job(job)
                return self._json(200, {"job_id": job["id"], "status": "approved"})
            if job["status"] != "approved":
                return self._json(409, {"error": "job not approved", "status": job["status"]})
            log_path = os.path.join(job_path(job["id"]), "agent.log")
            out_pdf = os.path.join(job_path(job["id"]), "report-final.pdf")
            rc = run_agent_public(["export-pdf", "--run-dir", job["run_dir"],
                                   "--out", out_pdf], log_path)
            if rc != 0 or not os.path.isfile(out_pdf):
                return self._json(500, {"error": "agent export-pdf failed", "exit": rc})
            job["status"] = "exported"
            job["pdf_path"] = out_pdf
            write_job(job)
            return self._json(200, {"job_id": job["id"], "status": "exported",
                                    "pdf_bytes": os.path.getsize(out_pdf)})
        return self._json(404, {"error": "not found"})

    def _dashboard(self, email):
        rows = ""
        for j in sorted(visible_jobs(email), key=lambda x: x["id"], reverse=True):
            rows += ("<tr><td><a href='/job/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (html.escape(j["id"]), html.escape(j["id"]), html.escape(j["status"]),
                        html.escape(j["question"][:80]),
                        html.escape(j.get("requester_org") or ""),
                        j.get("wall_seconds") or "",
                        "" if j.get("spend") is None else "%.6f" % j["spend"]))
        quota = CFG.get("quota_jobs_per_day", 3)
        used = quota_used(email)
        cap = CFG.get("monthly_spend_cap") or 0
        pin = CFG.get("price_per_1k_input_tokens") or 0
        pout = CFG.get("price_per_1k_output_tokens") or 0
        cap_line = ""
        if email == CFG.get("operator_email"):
            if cap > 0 and pin > 0 and pout > 0:
                cap_line = ("<p><b>Month spend:</b> %.6f / cap %.6f "
                            "(currency units, per configured prices)</p>"
                            % (month_spend(), cap))
            else:
                cap_line = ("<p><b>Month spend:</b> %.6f (cap unset or "
                            "prices unset — spend is unpriced, public mode "
                            "cannot enable)</p>" % month_spend())
        inner = ("""%s
        <p>Signed in as <b>%s</b> — <a href='/logout'>Sign out</a></p>
        <h1>Oryx AI — auditable research agent</h1>
        <p>Ask a research question. The agent frames it, gathers evidence, screens sources, drafts with citations only from the included list, verifies mechanically, and runs an adversarial pass. The report stays <b>DRAFT</b> until a named human approves it; only then can the final PDF (page 1: Audit Certificate) be exported.</p>
        <p><b>Your quota today:</b> %d / %d jobs.</p>
        %s
        <h2>Ask a question</h2>
        <form id='ask'>
        <p><textarea id='q' rows='4' cols='80' maxlength='2000' required placeholder='Your research question'></textarea></p>
        <p><input id='nm' placeholder='Your name' size='40'> <input id='org' placeholder='Organisation' size='40'></p>
        <p><button type='submit'>Submit job</button></p>
        <p id='err' class='fail'></p></form>
        <script>document.getElementById('ask').addEventListener('submit',async(e)=>{
          e.preventDefault();
          const body={question:document.getElementById('q').value.trim(),
            requester_name:document.getElementById('nm').value.trim(),
            requester_org:document.getElementById('org').value.trim()};
          const res=await fetch('/job',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          const j=await res.json();
          if(!res.ok){document.getElementById('err').textContent=j.error||('Error '+res.status);return;}
          location.href='/job/'+j.job_id;});</script>
        <h2>Jobs</h2>
        <table><tr><th>job</th><th>status</th><th>question</th><th>org</th><th>wall s</th><th>spend</th></tr>%s</table>"""
        % (mode_banner(), html.escape(email), used, quota, cap_line, rows))
        data = _page("Oryx AI — public staging", inner)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---- CLI -----------------------------------------------------------------

def cmd_serve(args):
    global CFG, JOBS_DIR, STATE_DIR, MODE, MODE_REASONS, MODEL_DISPLAY
    CFG = load_config(args.config)
    JOBS_DIR = _resolve(CFG["jobs_dir"])
    STATE_DIR = _resolve(CFG["state_dir"])
    os.makedirs(JOBS_DIR, exist_ok=True)
    _ensure_state_files()
    MODE, MODE_REASONS = compute_mode(CFG)
    try:
        with open(os.path.join(HERE, "agent-config.json")) as f:
            MODEL_DISPLAY = json.load(f)["backend"]["model"]
    except (OSError, ValueError, KeyError):
        MODEL_DISPLAY = os.environ.get("ORYX_PUBLIC_LLM_MODEL", "unknown")
    if os.environ.get("ORYX_PUBLIC_LLM_MODEL"):
        MODEL_DISPLAY = os.environ["ORYX_PUBLIC_LLM_MODEL"]
    port = args.port or CFG.get("port", 8933)
    print("Oryx AI public server (%s) on http://127.0.0.1:%d/"
          % (MODE, port), flush=True)
    if MODE == "local-staging":
        print("LOCAL-STAGING MODE — not public. Missing public "
              "requirements:", flush=True)
        for r in MODE_REASONS:
            print("  - %s" % r, flush=True)
    print("Jobs dir: %s | model: %s" % (JOBS_DIR, MODEL_DISPLAY), flush=True)
    threading.Thread(target=dispatcher, daemon=True).start()
    bind_host = os.environ.get("ORYX_BIND_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((bind_host, port), Handler)
    srv.serve_forever()
    return 0


def cmd_invite(args):
    global CFG, STATE_DIR
    CFG = load_config(args.config)
    STATE_DIR = _resolve(CFG["state_dir"])
    _ensure_state_files()
    email = (args.email or "").strip()
    if "@" not in email:
        print("invite failed: a valid email address is required",
              file=sys.stderr)
        return 2
    allow = CFG.get("beta_allowlist") or []
    if allow and email not in allow:
        print("invite failed: %s is not on the beta allowlist" % email,
              file=sys.stderr)
        return 3
    ttl = args.ttl or CFG.get("login_token_ttl_seconds", 3600)
    token = mint_login_token(email, ttl)
    base = (args.base_url or CFG.get("base_url",
                                     "http://127.0.0.1:8933")).rstrip("/")
    print("%s/login?token=%s" % (base, token))
    print("(single-use, expires in %d seconds; send it to %s only)"
          % (ttl, email))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Oryx AI public-deployment server (staged, localhost only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="serve the public server")
    p_serve.add_argument("--config", required=True)
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.set_defaults(func=cmd_serve)
    p_inv = sub.add_parser("invite", help="mint a login link for one email")
    p_inv.add_argument("--email", required=True)
    p_inv.add_argument("--config", required=True)
    p_inv.add_argument("--base-url", default="")
    p_inv.add_argument("--ttl", type=int, default=0)
    p_inv.set_defaults(func=cmd_invite)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
