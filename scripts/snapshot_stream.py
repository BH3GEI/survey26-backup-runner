#!/usr/bin/env python3
"""Write a FULL platform snapshot as an uncompressed tar stream to stdout -- nothing touches the
runner disk in plaintext (members are built in memory; git mirrors live in RAM, $ARCHIVE_RAMDIR).
The workflow pipes stdout through zstd | age | split, so only ciphertext is ever written.

Production safety: read-only everywhere; pg_dump only takes ACCESS SHARE locks and gives up after
30 s instead of queueing behind DDL (--lock-wait-timeout); storage reads 4 at a time, git 2 at a time.

Members: db/full.dump (pg_dump -Fc of the whole DB), db/counts.tsv, buckets/<bucket>/<name> (every
object of every storage bucket), repos/<org>/<repo>.bundle (every AGENTIC-OBSERVER26-runner-* repo,
all refs), repos/contestant/<url>.bundle (contestant repos of never-materialized repository
revisions), objects.jsonl, repos.jsonl, contestant_repos.jsonl, SHA256SUMS, MANIFEST.json.
Progress/diagnostics go to stderr (sealed by the workflow). A short counts-only summary is written
to $SNAPSHOT_SUMMARY for the (private) release notes. Exit 1 if any object/referenced commit is missing."""
import concurrent.futures as cf, datetime, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time
import urllib.error, urllib.parse, urllib.request

PG, URL, KEY, TOKEN = os.environ["PGCONN"], os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"], os.environ["GH_TOKEN"]
RAM = os.environ["ARCHIVE_RAMDIR"]
out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT)
sums: list[str] = []
now = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def add(name: str, data: bytes):
    ti = tarfile.TarInfo(name); ti.size = len(data); ti.mtime = now; ti.mode = 0o644
    out.addfile(ti, io.BytesIO(data))
    sums.append(f"{hashlib.sha256(data).hexdigest()}  ./{name}")


def sh(*a, **k):
    return subprocess.run(a, check=True, capture_output=True, **k).stdout


def psql(q: str) -> str:
    return sh("psql", PG, "-Atc", q, text=True)


# ---- database
dump = sh("pg_dump", PG, "--lock-wait-timeout=30s", "-Fc", "-Z", "0")
toc = subprocess.run(["pg_restore", "-l"], input=dump, capture_output=True, check=True).stdout.decode()
if " TABLE DATA public observer_runs " not in toc:
    sys.exit("dump incomplete")
add("db/full.dump", dump); del dump
counts_sql = psql("select string_agg(format('select %L, count(*) from %I.%I', n.nspname||'.'||c.relname, n.nspname, c.relname), "
                  "' union all ' order by 1) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') "
                  "and not c.relispartition and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_%' "
                  "and has_table_privilege(c.oid, 'select')")
counts = sh("psql", PG, "-At", "-F", "\t", "-c", counts_sql)
add("db/counts.tsv", counts)
counts = dict(l.split("\t") for l in counts.decode().splitlines())
log(f"db: {len(counts)} tables")

# ---- storage (in memory, bounded batches)
rows = [json.loads(l) for l in psql("select json_build_object('b',bucket_id,'n',name,'s',(metadata->>'size')::bigint) "
                                    "from storage.objects where name is not null").splitlines()]


def get(r):
    req = urllib.request.Request(f"{URL}/storage/v1/object/{r['b']}/" + urllib.parse.quote(r["n"], safe="/"),
                                 headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return r, resp.read(), "ok"
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) and attempt >= 1:
                return r, None, f"gone-{e.code}"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5 * (attempt + 1))
    return r, None, "failed"


objs = []
with cf.ThreadPoolExecutor(4) as ex:  # gentle on production storage
    for i in range(0, len(rows), 16):
        for r, body, st in ex.map(get, rows[i:i + 16]):
            rec = {**r, "status": st}
            if body is not None:
                add(f"buckets/{r['b']}/{r['n']}", body)
                rec.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            objs.append(rec)
add("objects.jsonl", "".join(json.dumps(o) + "\n" for o in objs).encode())
failed_objs = sum(o["status"] == "failed" for o in objs)
log(f"storage: {len(rows)} listed, {sum(o['status'] == 'ok' for o in objs)} ok, {failed_objs} failed")


# ---- git (mirrors in RAM, bundle bytes into the tar)
def bundle(url: str, env=None):
    tmp = tempfile.mkdtemp(dir=RAM)
    try:
        for _ in range(3):
            shutil.rmtree(tmp + "/m", ignore_errors=True)
            if subprocess.run(["git", "clone", "-q", "--mirror", url, tmp + "/m"], capture_output=True,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}).returncode == 0:
                break
        else:
            return None, None, set()
        head = subprocess.run(["git", "-C", tmp + "/m", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
        shas = set(subprocess.run(["git", "-C", tmp + "/m", "rev-list", "--all"], capture_output=True, text=True).stdout.split())
        if not shas:
            return b"", head, shas
        return sh("git", "-C", tmp + "/m", "bundle", "create", "-", "--all"), head, shas
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


refs = psql("select archive_ref from private.observer_materializations where archive_ref like 'github:%' "
            "union all select result_path from public.observer_runs where result_path like 'github:%'").split()
need: dict[str, set] = {}
for r in refs:
    m = re.match(r"^github:([^/]+)/([^@]+)@([0-9a-f]{7,40})$", r)
    if m:
        need.setdefault(f"{m[1]}/{m[2]}", set()).add(m[3])
repos = set(need)
for org in sh("gh", "api", "user/orgs", "--paginate", "--jq", ".[].login", text=True).split():
    if org.startswith("AGENTIC-OBSERVER26-runner-"):
        repos |= set(sh("gh", "api", f"orgs/{org}/repos?per_page=100", "--paginate", "--jq", ".[].full_name", text=True).split())
repo_recs = []
auth = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + __import__("base64").b64encode(f"x-access-token:{TOKEN}".encode()).decode()}
with cf.ThreadPoolExecutor(2) as ex:
    names = sorted(repos)
    for i in range(0, len(names), 8):
        chunk = names[i:i + 8]
        for full, (data, head, shas) in zip(chunk, ex.map(lambda f: bundle(f"https://github.com/{f}.git", auth), chunk)):
            miss = sorted(s for s in need.get(full, ()) if not any(x.startswith(s) for x in shas)) if data is not None else sorted(need.get(full, ()))
            if data:
                add(f"repos/{full}.bundle", data)
            repo_recs.append({"repo": full, "status": "clone-failed" if data is None else ("empty" if not data else "ok"),
                              "referenced": len(need.get(full, ())), "missing": miss})
add("repos.jsonl", "".join(json.dumps(r) + "\n" for r in repo_recs).encode())
missing_commits = sum(len(r["missing"]) for r in repo_recs)
log(f"repos: {len(repo_recs)}, referenced commits {sum(r['referenced'] for r in repo_recs)}, missing {missing_commits}")

# ---- contestant repos of never-materialized repository revisions (public clone, no token)
crow = [json.loads(l) for l in psql("select json_build_object('id', rv.id, 'url', rv.source_location) from public.observer_revisions rv "
                                    "left join private.observer_materializations m on m.revision_id = rv.id "
                                    "where rv.source_kind = 'repository' and m.revision_id is null").splitlines()]
seen: dict[str, dict] = {}
crecs = []
for r in crow:
    url = re.sub(r"//[^/@]*@", "//", (r["url"] or "").strip())
    if url not in seen:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data, head, _ = bundle(url) if url.startswith("https://") else (None, None, set())
        name = "repos/contestant/" + re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1]) + ".bundle"
        if data:
            add(name, data)
        seen[url] = {"url": url, "bundle": name if data else None, "head": head, "fetched_at": ts,
                     "status": "ok" if data else "unreachable"}
    crecs.append({"revision_id": r["id"], **seen[url]})
add("contestant_repos.jsonl", "".join(json.dumps(c) + "\n" for c in crecs).encode())

# ---- manifest
summary = {"db_tables": len(counts), "db_rows": sum(int(v) for v in counts.values()),
           "objects_ok": sum(o["status"] == "ok" for o in objs), "objects_bytes": sum(o.get("bytes", 0) for o in objs),
           "objects_gone": sum(o["status"].startswith("gone") for o in objs), "objects_failed": failed_objs,
           "repos": len(repo_recs), "referenced_commits": sum(r["referenced"] for r in repo_recs),
           "referenced_commits_missing": missing_commits, "contestant_repo_revisions": len(crecs),
           "contestant_repo_revisions_bundled": sum(c["status"] == "ok" for c in crecs), "files": len(sums)}
add("SHA256SUMS", ("\n".join(sums) + "\n").encode())
add("MANIFEST.json", json.dumps(summary, indent=1).encode())
out.close()
sys.stdout.buffer.flush()
open(os.environ["SNAPSHOT_SUMMARY"], "w").write(json.dumps(summary))
log(json.dumps(summary))
sys.exit(1 if failed_objs or missing_commits else 0)
