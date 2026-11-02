#!/usr/bin/env python3
"""Oryx AI — Phase 1 gated pilot server (LOCAL PROTOTYPE, NOT PUBLIC).

Wraps agent.py v0.4 (ask -> human approve -> export-pdf) in a small
job-queue HTTP service so a pilot request becomes a tracked job instead
of a bare CLI run. This is deployment scaffolding, not a launch:

  * Binds 127.0.0.1 ONLY. It is reachable from this machine and nowhere
    else. Exposing it to a network is a separate, explicit decision.
  * NO authentication in this prototype. The operator runs it locally;
    requester details are recorded, not verified. Before ANY network
    exposure, add: email magic-link auth, per-user quotas, a global
    monthly LLM spend cap, and a billed LLM key (the agent currently
    runs through localhost free-tier routers, which must NOT serve the
    public). See chatbot-deployment-plan.md §7.
  * The human gate is preserved end to end: a job's report stays DRAFT
    until an operator approves it through /approve, and export-pdf
    refuses unapproved runs (agent.py enforces this, not the server).
  * One job runs at a time (FIFO dispatcher). A STOP file in the jobs
    directory is the kill switch: new jobs are refused while it exists.
  * Cost logging gap (honest): agent.py v0.4 does not persist token
    usage from the model response, so the job record logs model,
    wall-clock time, and stage files — NOT token counts. Token logging
    must be added to agent.py before Phase 1 pricing is set from it.

Usage:
  python3 pilot-server.py [--port 8931]
  curl localhost:8931/                      # dashboard + MVP web UI
  Open http://127.0.0.1:8931/ in a browser to submit a question, watch
  the job, read the DRAFT, approve it, and export the final PDF.
  curl -X POST localhost:8931/job -H 'Content-Type: application/json' \
      -d '{"question":"...","requester_name":"...","requester_org":"...","requester_email":"..."}'
  curl localhost:8931/jobs
  curl -X POST localhost:8931/job/<id>/approve -H 'Content-Type: application/json' -d '{"reviewer":"Riya Nalwa"}'
  curl -X POST localhost:8931/job/<id>/export
  curl -o report.pdf localhost:8931/job/<id>/pdf

Jobs live in ~/workspace/oryx-ai/pilot-jobs/<job-id>/.
Requires the agent's backend (local OmniRoute server) to be up.
"""

import datetime
import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "agent.py")
JOBS_DIR = os.path.join(HERE, "pilot-jobs")
STOP_FILE = os.path.join(JOBS_DIR, "STOP")
MAX_QUESTION_CHARS = 2000

os.makedirs(JOBS_DIR, exist_ok=True)

with open(os.path.join(HERE, "agent-config.json")) as f:
    MODEL = json.load(f)["backend"]["model"]

job_queue = queue.Queue()
jobs_lock = threading.Lock()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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


def run_agent(args, log_path):
    """Run one agent.py command; append combined output to log_path."""
    with open(log_path, "a") as lf:
        lf.write("\n$ python3 agent.py %s\n" % " ".join(args))
        lf.flush()
        proc = subprocess.run(
            [sys.executable, AGENT] + args,
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
            rc = run_agent(
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
        job["cost_note"] = ("model=%s; wall=%ss; token counts NOT recorded "
                            "by agent v0.4 (see pilot-server.py header)"
                            % (MODEL, job["wall_seconds"]))
        write_job(job)
        job_queue.task_done()


threading.Thread(target=dispatcher, daemon=True).start()


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


def job_html(job):
    """Browser view of one job. JSON clients keep using the JSON route."""
    jid = job["id"]
    st = job["status"]
    parts = ["<p><a href='/'>&larr; All jobs</a></p>",
             "<div class='banner'>LOCAL DEMO ONLY — this server binds 127.0.0.1 and has no auth. Never expose it to a network.</div>",
             "<h1>Job %s</h1>" % html.escape(jid),
             "<p><b>Status:</b> %s</p>" % html.escape(st)]
    if st == "failed":
        parts.append("<p class='fail'>This job FAILED — the log tail below states why. Nothing was faked to complete it.</p>")
    parts.append("<p><b>Question:</b> %s</p>" % html.escape(job["question"]))
    parts.append("<p><b>Requester:</b> %s (%s, %s)</p>" % (
        html.escape(job.get("requester_name") or ""),
        html.escape(job.get("requester_org") or ""),
        html.escape(job.get("requester_email") or "")))
    for k in ("created_at", "started_at", "finished_at", "wall_seconds", "agent_exit_code", "cost_note"):
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
    _audit_p = os.path.join(run_dir, "09-layer-audit.json") if run_dir else ""
    if _audit_p and os.path.isfile(_audit_p):
        import json as _json
        _a = _json.load(open(_audit_p))
        _rows = "".join("<tr><td>[S%d]</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            s["ref"], html.escape(s["url"][:70]), html.escape(s["onecite"]),
            html.escape(s["triage_verdict"])) for s in _a["sources"])
        parts.append("<h2>Verification layers (v2)</h2><p><b>Mechanical triage score:</b> %s/100 — "
            "this is NOT a claim-support score; claim support stays a human judgement. "
            "OneCite normalises DOI/arXiv/PMID sources against metadata; paper-verify scores the mechanical evidence.</p>"
            "<table><tr><th>source</th><th>url</th><th>onecite</th><th>triage</th></tr>%s</table>" % (
            html.escape(str(_a.get("paperverify_triage_score"))), _rows))
    if st == "needs_review":
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
    if st == "approved":
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

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if not parts:
            return self._dashboard()
        if parts == ["jobs"]:
            out = []
            for d in sorted(os.listdir(JOBS_DIR)):
                j = read_job(d)
                if j:
                    out.append({k: j.get(k) for k in
                                ("id", "status", "question", "requester_name",
                                 "requester_org", "created_at", "wall_seconds")})
            return self._json(200, out)
        if len(parts) == 2 and parts[0] == "job":
            job = read_job(parts[1])
            if job is None:
                return self._json(404, {"error": "unknown job"})
            accept = self.headers.get("Accept") or ""
            if "text/html" in accept:
                data = job_html(job)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json(200, job)
        if len(parts) == 3 and parts[0] == "job" and parts[2] == "draft":
            job = read_job(parts[1])
            if job is None or not job.get("run_dir"):
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
            if job is None or not job.get("pdf_path") or \
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
        if parts == ["job"]:
            if os.path.exists(STOP_FILE):
                return self._json(503, {"error": "pilot server stopped (STOP file present)"})
            body = self._read_json()
            if not body:
                return self._json(400, {"error": "JSON body required"})
            q = (body.get("question") or "").strip()
            email = (body.get("requester_email") or "").strip()
            if not q or len(q) > MAX_QUESTION_CHARS:
                return self._json(400, {"error": "question required, max %d chars" % MAX_QUESTION_CHARS})
            if "@" not in email:
                return self._json(400, {"error": "requester_email required"})
            job_id = "%s-%s" % (
                datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
                uuid.uuid4().hex[:6])
            os.makedirs(job_path(job_id), exist_ok=True)
            job = {
                "id": job_id, "status": "queued", "question": q,
                "requester_name": body.get("requester_name", ""),
                "requester_org": body.get("requester_org", ""),
                "requester_email": email, "model": MODEL,
                "created_at": now_iso(), "started_at": None,
                "finished_at": None, "wall_seconds": None,
                "agent_exit_code": None, "run_dir": None,
                "approval": None, "pdf_path": None,
            }
            write_job(job)
            job_queue.put(job_id)
            return self._json(201, {"job_id": job_id, "status": "queued"})
        if len(parts) == 3 and parts[0] == "job" and parts[2] in ("approve", "export"):
            job = read_job(parts[1])
            if job is None:
                return self._json(404, {"error": "unknown job"})
            action = parts[2]
            if action == "approve":
                body = self._read_json() or {}
                reviewer = (body.get("reviewer") or "").strip()
                if job["status"] != "needs_review":
                    return self._json(409, {"error": "job not awaiting review", "status": job["status"]})
                if not reviewer:
                    return self._json(400, {"error": "reviewer required"})
                log_path = os.path.join(job_path(job["id"]), "agent.log")
                rc = run_agent(["approve", "--run-dir", job["run_dir"],
                                "--reviewer", reviewer], log_path)
                if rc != 0:
                    return self._json(500, {"error": "agent approve failed", "exit": rc})
                job["status"] = "approved"
                job["approval"] = {"reviewer": reviewer, "at": now_iso()}
                write_job(job)
                return self._json(200, {"job_id": job["id"], "status": "approved"})
            # export
            if job["status"] != "approved":
                return self._json(409, {"error": "job not approved", "status": job["status"]})
            log_path = os.path.join(job_path(job["id"]), "agent.log")
            out_pdf = os.path.join(job_path(job["id"]), "report-final.pdf")
            rc = run_agent(["export-pdf", "--run-dir", job["run_dir"],
                            "--out", out_pdf], log_path)
            if rc != 0 or not os.path.isfile(out_pdf):
                return self._json(500, {"error": "agent export-pdf failed", "exit": rc})
            job["status"] = "exported"
            job["pdf_path"] = out_pdf
            write_job(job)
            return self._json(200, {"job_id": job["id"], "status": "exported",
                                    "pdf_bytes": os.path.getsize(out_pdf)})
        return self._json(404, {"error": "not found"})

    def _dashboard(self):
        rows = ""
        for d in sorted(os.listdir(JOBS_DIR), reverse=True):
            j = read_job(d)
            if not j:
                continue
            rows += ("<tr><td><a href='/job/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (html.escape(j["id"]), html.escape(j["id"]), html.escape(j["status"]),
                        html.escape(j["question"][:80]),
                        html.escape(j.get("requester_org") or ""),
                        j.get("wall_seconds") or ""))
        inner = ("""<div class='banner'>LOCAL DEMO ONLY — this server binds 127.0.0.1 and has no auth. Never expose it to a network.</div>
        <h1>Oryx AI — auditable research agent (MVP)</h1>
        <p>Ask a research question. The agent frames it, gathers evidence, screens sources, drafts with citations only from the included list, verifies mechanically, and runs an adversarial pass. The report stays <b>DRAFT</b> until a named human approves it; only then can the final PDF (page 1: Audit Certificate) be exported.</p>
        <h2>Ask a question</h2>
        <form id='ask'>
        <p><textarea id='q' rows='4' cols='80' maxlength='2000' required placeholder='Your research question'></textarea></p>
        <p><input id='nm' placeholder='Your name' size='40'> <input id='org' placeholder='Organisation' size='40'></p>
        <p><input id='em' type='email' placeholder='Email (required)' size='40' required></p>
        <p><button type='submit'>Submit job</button></p>
        <p id='err' class='fail'></p></form>
        <script>document.getElementById('ask').addEventListener('submit',async(e)=>{
          e.preventDefault();
          const body={question:document.getElementById('q').value.trim(),
            requester_name:document.getElementById('nm').value.trim(),
            requester_org:document.getElementById('org').value.trim(),
            requester_email:document.getElementById('em').value.trim()};
          const res=await fetch('/job',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          const j=await res.json();
          if(!res.ok){document.getElementById('err').textContent=j.error||('Error '+res.status);return;}
          location.href='/job/'+j.job_id;});</script>
        <h2>How it works</h2>
        <ol><li>Framing — sub-questions, scope, exclusions, false-premise check</li>
        <li>Evidence — live retrieval, or an operator-curated source list</li>
        <li>Screening — per-source include/exclude with a logged reason</li>
        <li>Synthesis — draft citing ONLY the included sources</li>
        <li>Evidence table — one structured row per included source</li>
        <li>Mechanical verification — markers resolve, claims cited, failures marked UNVERIFIED in place</li>
        <li>Adversarial review — a separate pass that FLAGS problems, never fixes them</li>
        <li>Human gate — a named reviewer approves; no approval, no final PDF</li>
        <li>Export — final PDF with a page-1 Audit Certificate (mechanical figures only)</li></ol>
        <p>See a <a href='https://oryxcentre.dpdns.org/oryx-ai-sample-report.html'>sample report</a> from our own launch test. Limits: token counts are not recorded by this build; live retrieval can fail on shared-IP budgets (a failed job says so in its log); this demo carries no pricing and no payment step — pilot terms are agreed in writing first.</p>
        <h2>Jobs</h2>
        <table><tr><th>job</th><th>status</th><th>question</th><th>org</th><th>wall s</th></tr>%s</table>""" % rows)
        data = _page("Oryx AI — MVP (local)", inner)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = 8931
    if len(sys.argv) > 2 and sys.argv[1] == "--port":
        port = int(sys.argv[2])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Oryx AI pilot server (LOCAL ONLY) on http://127.0.0.1:%d/" % port,
          flush=True)
    print("Jobs dir: %s | model: %s" % (JOBS_DIR, MODEL), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
