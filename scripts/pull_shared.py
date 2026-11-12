"""Mirror the team's shared dataset into <POLYDATA_DIR>/shared over Tailscale + SFTP, then verify it.

    python scripts/pull_shared.py            pull what is new, then verify
    python scripts/pull_shared.py --verify   verify only what is already here against the remote
    python scripts/pull_shared.py --offline  describe the local copy without connecting

Safe to re-run: a file that already exists locally with the remote's size is skipped. Files are
downloaded as <name>.part and renamed when complete, so an interrupted run leaves nothing half-written.

The password is not in the repo. It is read from the environment variable SHARED_SFTP_PASSWORD,
or from the line SHARED_SFTP_PASSWORD=... in <POLYDATA_DIR>/shared_sftp.env. Host, user and port
can be overridden the same way (SHARED_SFTP_HOST, SHARED_SFTP_USER, SHARED_SFTP_PORT).
The server's host key is remembered in <POLYDATA_DIR>/shared_known_hosts on first contact and
checked on every later run.
"""
import argparse
import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import config, load_shared  # noqa: E402

DEST = load_shared.SHARED_DIR
ENV_FILE = os.path.join(config.DATA_DIR, "shared_sftp.env")
KNOWN_HOSTS = os.path.join(config.DATA_DIR, "shared_known_hosts")
DEFAULTS = {"SHARED_SFTP_HOST": "", "SHARED_SFTP_USER": "", "SHARED_SFTP_PORT": "22"}      # host and user come from the env file


def settings():
    values = dict(DEFAULTS)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            for line in f:
                key, sep, value = line.rstrip("\r\n").partition("=")
                if sep and not key.lstrip().startswith("#"):
                    values[key.strip()] = value
    for key in list(DEFAULTS) + ["SHARED_SFTP_PASSWORD"]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [k for k in ("SHARED_SFTP_HOST", "SHARED_SFTP_USER") if not values.get(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}: put them in {ENV_FILE} (this repo is public, so they are not in the code)")
    if not values.get("SHARED_SFTP_PASSWORD"):
        sys.exit(f"No password: set SHARED_SFTP_PASSWORD or put SHARED_SFTP_PASSWORD=... in {ENV_FILE}")
    return values


def connect():
    import paramiko

    s = settings()
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        open(KNOWN_HOSTS, "a").close()
        client.load_host_keys(KNOWN_HOSTS)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())   # first contact: remember the key
    try:
        client.connect(s["SHARED_SFTP_HOST"], port=int(s["SHARED_SFTP_PORT"]), username=s["SHARED_SFTP_USER"],
                       password=s["SHARED_SFTP_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=20)
    except Exception as e:
        if os.path.exists(KNOWN_HOSTS) and os.path.getsize(KNOWN_HOSTS) == 0:
            os.remove(KNOWN_HOSTS)
        sys.exit(f"Could not connect to {s['SHARED_SFTP_USER']}@{s['SHARED_SFTP_HOST']}:{s['SHARED_SFTP_PORT']}: "
                 f"{type(e).__name__}: {e}\nCheck `tailscale status` and `tailscale ping {s['SHARED_SFTP_HOST']}`.")
    return client, client.open_sftp()


def remote_files(sftp, folder="."):
    """{relative path with '/': (size, mtime)} for every file below folder."""
    out = {}
    for entry in sftp.listdir_attr(folder):
        path = entry.filename if folder == "." else f"{folder}/{entry.filename}"
        if stat.S_ISDIR(entry.st_mode):
            out.update(remote_files(sftp, path))
        elif stat.S_ISREG(entry.st_mode):
            out[path] = (entry.st_size, entry.st_mtime)
    return out


def local_path(rel):
    path = os.path.normpath(os.path.join(DEST, *rel.split("/")))
    if not path.startswith(os.path.normpath(DEST) + os.sep):
        raise ValueError(f"remote path escapes the shared folder: {rel!r}")
    return path


def local_files():
    out = {}
    for folder, _, names in os.walk(DEST):
        for n in names:
            if not n.endswith(".part"):
                path = os.path.join(folder, n)
                out[os.path.relpath(path, DEST).replace(os.sep, "/")] = os.path.getsize(path)
    return out


def mb(n):
    return f"{n / 1e6:,.1f} MB"


def pull(sftp, remote):
    todo = [(rel, size, mtime) for rel, (size, mtime) in sorted(remote.items())
            if not (os.path.exists(local_path(rel)) and os.path.getsize(local_path(rel)) == size)]
    total = sum(size for _, size, _ in todo)
    print(f"Remote: {len(remote)} files, {mb(sum(s for s, _ in remote.values()))}. "
          f"To download: {len(todo)} files, {mb(total)}; {len(remote) - len(todo)} already here.")
    done, t0 = 0, time.time()
    for i, (rel, size, mtime) in enumerate(todo, 1):
        path = local_path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sftp.get(rel, path + ".part")
        got = os.path.getsize(path + ".part")
        if got != size:            # the remote file was still being written; the next run fetches it again
            print(f"  [{i}/{len(todo)}] {rel}: got {got:,} bytes, listing said {size:,}; left for the next run")
            os.remove(path + ".part")
            continue
        os.replace(path + ".part", path)
        if mtime:
            os.utime(path, (mtime, mtime))
        done += size
        rate = done / max(time.time() - t0, 1e-9)
        print(f"  [{i}/{len(todo)}] {rel}  {mb(size)}  ({mb(done)} of {mb(total)}, {rate / 1e6:.1f} MB/s)")


def verify(remote):
    """Print every local file with its size (and the remote's), then describe each Parquet. Returns problems."""
    local = local_files()
    problems = []
    print(f"\nFiles in {DEST}")
    for rel in sorted(set(local) | set(remote or {})):
        have = local.get(rel)
        if remote is None:
            note = ""
        elif rel not in remote:
            note = "   not on the remote"
        elif have is None:
            note, _ = f"   MISSING locally (remote {remote[rel][0]:,})", problems.append(rel)
        elif have != remote[rel][0]:
            note, _ = f"   SIZE DIFFERS (remote {remote[rel][0]:,})", problems.append(rel)
        else:
            note = "   = remote"
        print(f"  {have if have is not None else 0:>15,}  {rel}{note}")
    print(f"  {sum(local.values()):>15,}  total in {len(local)} files"
          + ("" if remote is None else f"; {len(problems)} missing or different from the remote"))

    print("\nParquet files")
    for rel in sorted(r for r in local if r.lower().endswith(".parquet")):
        try:
            d = load_shared.describe_parquet(local_path(rel))
        except Exception as e:
            print(f"  {rel}\n    UNREADABLE: {type(e).__name__}: {str(e).splitlines()[0][:100]}")
            problems.append(rel)
            continue
        span = f"{d['first']} to {d['last']} ({d['time_col']})" if d["time_col"] else "no timestamp column found"
        print(f"  {rel}\n    rows {d['rows']:,}   {span}")
        print("    columns: " + ", ".join(f"{n} [{t}]" for n, t in d["columns"]))

    inventory = os.path.join(DEST, "DATA_INVENTORY.md")
    print("\nDATA_INVENTORY.md, five-line summary")
    if os.path.exists(inventory):
        with open(inventory, encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip() for line in f if line.strip()]
        heading = next((i for i, line in enumerate(lines) if line.lstrip("# ").lower() == "summary"), -1)
        print("\n".join("  " + line for line in lines[heading + 1: heading + 6]))
    else:
        print("  not present")
    return problems


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verify", action="store_true", help="do not download, only compare with the remote")
    ap.add_argument("--offline", action="store_true", help="do not connect, only describe the local copy")
    args = ap.parse_args()
    os.makedirs(DEST, exist_ok=True)
    remote = None
    if not args.offline:
        client, sftp = connect()
        try:
            remote = remote_files(sftp)
            if not args.verify:
                pull(sftp, remote)
                remote = remote_files(sftp)        # sizes as of the end of the pull
        finally:
            client.close()
    problems = verify(remote)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
