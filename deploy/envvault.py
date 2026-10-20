#!/usr/bin/env python3
"""Keep a stack's .env in Bitwarden Secrets Manager.

  envvault.py push   <STACK> <env file> <project id> <template out>
  envvault.py render <template> [out]          (out: write mode 600; default stdout)
  envvault.py verify <template> <env file>     (render and compare byte for byte)
  envvault.py get    <NAME>                    (one value to stdout -- for pipes only)

Each KEY=VALUE line becomes the secret <STACK>__<KEY>. The template keeps everything
else (comments, blank lines, order) and has KEY={{vault:<STACK>__<KEY>}} in place of the
value, so it holds no secrets. Values are never printed. Needs BWS_ACCESS_TOKEN (or
BWS_TOKEN_FILE) and the bws CLI.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

PH = re.compile(rb"\{\{vault:([A-Za-z0-9_]+)\}\}")
_home = tempfile.mkdtemp(prefix="bws-")


def _token():
    t = os.environ.get("BWS_ACCESS_TOKEN")
    if not t and os.environ.get("BWS_TOKEN_FILE"):
        t = open(os.environ["BWS_TOKEN_FILE"]).read().strip()
    if not t:
        sys.exit("no BWS_ACCESS_TOKEN / BWS_TOKEN_FILE")
    return t


def bws(*args):
    tok = _token()
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": _home, "BWS_ACCESS_TOKEN": tok}
    for attempt in range(4):
        r = subprocess.run(["bws", "--output", "json", "--color", "no", *args],
                           env=env, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            # strict=False: secret values (SSH keys...) can hold raw line breaks, which some bws
            # builds emit unescaped -- strict JSON would refuse the whole listing
            return json.loads(r.stdout or "null", strict=False)
        err = (r.stderr or "").replace(tok, "<token>")
        last = next((l for l in reversed(err.splitlines()) if "error" in l.lower()), err[-200:])
        time.sleep(2 * (attempt + 1))
    sys.exit(f"bws {args[0]} {args[1]} failed: {last[:200]}")


def all_secrets():
    return {s["key"]: s for s in bws("secret", "list") or []}


def push(stack, env_path, project, tpl_path):
    raw = open(env_path, "rb").read()
    have = all_secrets()
    out, created, updated, same = [], 0, 0, 0
    for line in raw.split(b"\n"):
        s = line.strip()
        if not s or s.startswith(b"#") or b"=" not in line:
            out.append(line)
            continue
        k, v = line.split(b"=", 1)
        if not v:
            out.append(line)
            continue
        name = f"{stack}__{k.decode().strip()}"
        val = v.decode()
        cur = have.get(name)
        if cur is None:
            bws("secret", "create", "--", name, val, project)  # positionals after -- (values may start with '-')
            created += 1
        elif cur.get("value") != val or cur.get("projectId") != project:
            bws("secret", "edit", f"--value={val}", f"--project-id={project}", "--", cur["id"])
            updated += 1
        else:
            same += 1
        out.append(k + b"={{vault:" + name.encode() + b"}}")
        time.sleep(0.5)
    with open(tpl_path, "wb") as f:
        f.write(b"\n".join(out))
    print(f"{stack}: {created} created, {updated} updated, {same} unchanged -> template {tpl_path}")


def render(tpl_path):
    tpl = open(tpl_path, "rb").read()
    have = all_secrets()
    missing = sorted({m.decode() for m in PH.findall(tpl) if m.decode() not in have})
    if missing:
        sys.exit(f"missing in vault: {', '.join(missing)}")
    return PH.sub(lambda m: have[m.group(1).decode()]["value"].encode(), tpl)


def main():
    cmd, *a = sys.argv[1:] or ["help"]
    if cmd == "push" and len(a) == 4:
        push(*a)
    elif cmd == "render" and len(a) in (1, 2):
        data = render(a[0])
        if len(a) == 2:
            fd = os.open(a[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, data)
            os.close(fd)
            os.chmod(a[1], 0o600)
        else:
            sys.stdout.buffer.write(data)
    elif cmd == "get" and len(a) == 1:
        s = all_secrets().get(a[0])
        if s is None:
            sys.exit(f"{a[0]} is not in the vault")
        sys.stdout.write(s["value"])
    elif cmd == "verify" and len(a) == 2:
        same = render(a[0]) == open(a[1], "rb").read()
        print(f"{a[1]}: {'IDENTICAL' if same else 'DIFFERENT'}")
        sys.exit(0 if same else 1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
